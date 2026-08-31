import { useState, useEffect, useCallback } from 'react'
import { X, ShieldCheck, RefreshCw, Target, Filter, Gauge } from 'lucide-react'

interface Props {
  onClose: () => void
}

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface OutcomeStats {
  count: number
  wins?: number
  losses?: number
  win_rate_pct: number | null
  avg_r?: number | null
  expectancy_r: number | null
  sum_pnl_usd?: number
  tp1_hit_rate_pct: number | null
  tp2_hit_rate_pct: number | null
  by_status: Record<string, number>
}

interface GateItem {
  gate: string
  count: number
  last_reason: string | null
  last_symbol: string | null
  last_seen: string | null
}

interface Assertiveness {
  enabled: boolean
  reason?: string
  window_days?: number
  real_money?: OutcomeStats
  shadow?: OutcomeStats
  gates?: { window_days: number; total_skips: number; items: GateItem[] }
  calibration?: {
    mature: boolean
    total_resolved: number
    min_sample: number
    p_global: number | null
    win_rate_pct: number | null
    computed_at: string | null
  }
  gate_counterfactual?: {
    window_days: number
    quality_edge?: CfBlock
    regime_sizing?: CfBlock
    pre_tp1_protect?: CfBlock
    conviction_up?: CfBlock
    edge_sizing?: CfBlock
  }
  pyramiding_opportunity?: { verdict?: string | null; continuation_rate_pct?: number | null; tp1_reached?: number }
  hedge_by_regime?: { verdict?: string | null; short?: { avg_r?: number | null; count?: number }; n_tagged?: number }
  p05?: P05Block
  computed_at?: string
}

// ── P05: evidência governada (somente leitura — nenhuma ação de promoção) ──
interface P05Segment {
  key: string
  count: number
  avg_r: number | null
  win_rate_pct: number | null
  total_r: number | null
  reliability: string
}

interface P05Metrics {
  count?: number
  expectancy_r?: number | null
  profit_factor?: number | null
  max_drawdown_r?: number | null
  win_rate_pct?: number | null
}

interface P05Experiment {
  id?: number
  status?: string
  objective?: string
  candidate_config?: Record<string, unknown> | null
  shadow_metrics?: {
    challenger_resolved?: number
    observed_days?: number
    coverage_pct?: number | null
    champion?: P05Metrics
    candidate?: P05Metrics
    added_ops?: number
    avoided_ops?: number
  } | null
  decision?: { verdict?: string | null; reason_code?: string | null; detail?: string | null } | null
}

interface P05Block {
  enabled?: boolean
  reason?: string
  evidence_quality?: {
    shadow_resolved?: number
    min_offline_required?: number
    maturity?: string
    ready_for_candidates?: boolean
    features_usable?: string[]
    features_below_coverage?: string[]
    excluded_total?: number | null
  }
  segments?: Record<string, { best?: P05Segment[]; worst?: P05Segment[] }>
  gate_events?: {
    total_events?: number
    by_phase?: Record<string, { events?: number | null; reason?: string }>
    disclaimer?: string
  }
  shadow_experiment?: P05Experiment | null
  eligible_experiment?: P05Experiment | null
  mae_mfe?: MaeMfeBlock
  telemetry?: TelemetryBlock
}

// ── P05.1T: telemetria prospectiva (observação pura) ───────────────────────
interface Dist {
  mean?: number | null
  median?: number | null
  p75?: number | null
  p90?: number | null
  count?: number
}

interface MaeMfeBlock {
  status?: string
  observed?: number
  eligible_resolved?: number
  coverage_pct?: number | null
  missing?: number
  min_observed?: number
  min_coverage_pct?: number
  mae_r?: Dist
  mfe_r?: Dist
}

interface AxisCoverage {
  coverage_global_pct?: number | null
  coverage_train_pct?: number | null
  present?: number
  missing?: number
  status?: string
}

interface TelemetryBlock {
  context_coverage?: {
    min_required_pct?: number
    window_days?: number
    axes?: Record<string, AxisCoverage>
  }
  slippage?: {
    total_real_closed?: number
    slippage_valid?: number
    coverage_pct?: number | null
    mean?: number | null
    median?: number | null
    p90?: number | null
  }
  latency?: { status?: string; reason?: string }
  retention?: { observed_retention_days?: number | null; history_note?: string }
}

const TELEMETRY_LABEL: Record<string, string> = {
  UNAVAILABLE: 'sem dados ainda',
  COLLECTING: 'coletando',
  USABLE: 'utilizável',
}

// ── P05.1R: prontidão para NOVA AVALIAÇÃO (somente leitura) ────────────────
interface ReadinessExperiment {
  experiment_id?: number
  axis?: string
  value?: string
  action?: string
  score_delta?: number | null
  failure_classification?: string
  failure_reason?: string
  readiness?: string
  blockers?: string[]
  missing_to_minimum?: number
  minimum_affected?: number
  daily_rate?: number | null
  eta_days?: number | null
  eta_reason?: string
  unknown_pct?: number | null
  current_window?: { context_coverage_pct?: number | null; affected?: number }
  prospective?: {
    prospective_validation_affected?: number
    prospective_test_affected?: number
    prospective_candidate_oos_count?: number
  }
}

interface Readiness {
  ok?: boolean
  holdout_status?: string
  summary?: {
    monitored?: number
    readiness_by_status?: Record<string, number>
    next_recommended_check_days?: number | null
    next_recommended_check_reason?: string
  }
  retention?: {
    observed_retention_days?: number | null
    retention_warning?: string | null
    history_status?: string
    history_note?: string
    rows_older_than_90d?: number
  }
  experiments?: ReadinessExperiment[]
}

const READINESS_LABEL: Record<string, string> = {
  WAITING_FOR_DATA: 'juntando dados',
  READY_FOR_REEVALUATION: 'amostra suficiente para reavaliar',
  REFUTED_LAST_RUN: 'hipótese contrariada',
  MIXED_EVIDENCE: 'evidência mista',
  RETENTION_AT_RISK: 'risco de retenção',
  INSUFFICIENT_METADATA: 'metadados insuficientes',
  UNKNOWN: 'indefinido',
}

const READINESS_COLOR: Record<string, string> = {
  WAITING_FOR_DATA: 'text-yellow-300',
  READY_FOR_REEVALUATION: 'text-emerald-300',
  REFUTED_LAST_RUN: 'text-red-300',
  MIXED_EVIDENCE: 'text-orange-300',
  RETENTION_AT_RISK: 'text-red-300',
  INSUFFICIENT_METADATA: 'text-slate-400',
  UNKNOWN: 'text-slate-400',
}

const RELIABILITY_LABEL: Record<string, string> = {
  INSUFFICIENT: 'amostra insuficiente',
  EARLY: 'início',
  USABLE: 'utilizável',
  STRONG: 'forte',
}

const SEGMENT_AXIS_LABEL: Record<string, string> = {
  by_tier: 'Por tier',
  by_timeframe: 'Por timeframe',
  by_direction: 'Por direção',
  by_session_utc: 'Por sessão (UTC)',
  by_regime: 'Por regime',
  by_score_bin: 'Por faixa de score',
  by_atr_band: 'Por volatilidade (ATR)',
}

// Bloco genérico do contrafactual: o que importa pra UI é enabled_now + verdict.
interface CfBlock {
  enabled_now?: boolean
  verdict?: string | null
  [k: string]: unknown
}

// gate → rótulo PT-BR (espelha gateLabel do RecommendationsPanel)
const GATE_LABEL: Record<string, string> = {
  'liquidity-gate': 'liquidez baixa',
  'prob-gate': 'P(TP1) baixa',
  'rr-gate': 'R:R fraco',
  'score-min': 'score abaixo do mínimo',
  'proximity': 'preço longe da entrada',
  'atr-gate': 'volatilidade fora da faixa',
  'exec-universe': 'fora do universo de execução',
  'blacklist': 'símbolo bloqueado',
  'time-block': 'janela de horário bloqueada',
  'funding-gate': 'funding extremo',
  'mtf-gate': 'timeframes desalinhados',
  'entry-throttle': 'limite de entradas/hora',
  'direction-cap': 'limite de posições na direção',
  'cluster-cap': 'limite do cluster',
  'cluster-cap-dir': 'limite do cluster (direção)',
  'symbol-sl-cooldown': 'cooldown pós-stop no símbolo',
  'regime-guard': 'guarda de regime (stops recentes)',
  'daily-sl-breaker': 'breaker diário de stops',
  'flip-advisory': 'flip recente (advisory)',
  'risk-budget': 'orçamento de risco agregado',
  'news-gate': 'blackout de notícias (FOMC/CPI)',
  'fill-rr': 'R:R fraco no preço de fill',
  'maker-no-fill': 'ordem maker não preencheu',
  'funding-ev': 'funding drena o EV',
  'struct_chase': 'perna esticada desde a base',
  'quality-edge-gate': 'score marginal sem edge',
  'size-damp': 'notional pós-damping abaixo do mínimo',
  'liq-tier': 'notional pós-tier de liquidez abaixo do mínimo',
  'regime-size': 'notional pós-regime abaixo do mínimo',
  'filler-fora': 'filler fora da allowlist sem slot',
  'ptp-daily-target': 'meta diária de lucro atingida',
}

function gateLabel(g: string): string {
  return GATE_LABEL[g] || g
}

function fmtR(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–'
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}R`
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–'
  return `${n.toFixed(1)}%`
}

function rColor(n: number | null | undefined): string {
  if (n === null || n === undefined) return 'text-slate-300'
  return n > 0 ? 'text-emerald-300' : n < 0 ? 'text-red-300' : 'text-slate-300'
}

export default function AssertivenessPanel({ onClose }: Props) {
  const [data, setData] = useState<Assertiveness | null>(null)
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(30)

  const load = useCallback(async (d: number) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${BACKEND}/api/shadow/assertiveness?days=${d}&gate_days=7`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  // P05.1R — monitor de prontidão (GET somente leitura, degrada em silêncio).
  const loadReadiness = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND}/api/strategy/p05/readiness?days=120&limit=20`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setReadiness(await res.json())
    } catch {
      setReadiness(null)
    }
  }, [])

  useEffect(() => { load(days) }, [load, days])
  useEffect(() => { loadReadiness() }, [loadReadiness])

  const real = data?.real_money
  const shadow = data?.shadow
  const gates = data?.gates
  const calib = data?.calibration
  const cf = data?.gate_counterfactual
  const pyr = data?.pyramiding_opportunity
  const hedge = data?.hedge_by_regime
  const p05 = data?.p05
  const eq = p05?.evidence_quality
  // Após a decisão o experimento sai de SHADOW; manter o último ELIGIBLE evita
  // que o resultado e o plano governado desapareçam do painel.
  const exp = p05?.shadow_experiment ?? p05?.eligible_experiment
  const sm = exp?.shadow_metrics

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-4xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2 min-w-0">
            <ShieldCheck className="w-5 h-5 text-emerald-400 shrink-0" />
            <h2 className="text-base font-bold text-white truncate">Assertividade do Bot</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">· o quão confiável está sendo</span>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <div className="hidden sm:flex items-center gap-1 mr-1">
              {[7, 30, 90].map(d => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={`px-2 py-1 rounded text-[11px] font-semibold border transition-colors ${
                    days === d
                      ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
                      : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'
                  }`}
                >{d}d</button>
              ))}
            </div>
            <button onClick={() => load(days)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-2 hover:bg-slate-800 rounded shrink-0" aria-label="Fechar">
              <X className="w-5 h-5 text-slate-300" />
            </button>
          </div>
        </div>

        {/* Mobile day picker */}
        <div className="flex sm:hidden items-center gap-1 px-4 py-2 border-b border-slate-800">
          {[7, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-3 py-1 rounded text-[11px] font-semibold border transition-colors ${
                days === d
                  ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
                  : 'bg-slate-800 border-slate-700 text-slate-400'
              }`}
            >{d} dias</button>
          ))}
        </div>

        {error && (
          <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">⚠ {error}</div>
        )}

        {data && !data.enabled && (
          <div className="m-4 p-4 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200">
            ⚠ {data.reason || 'Banco de dados não configurado.'}
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-4">
          {loading && !data && (
            <div className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {data?.enabled && (
            <>
              {/* ── Dinheiro real (source=auto) ─────────────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Target className="w-4 h-4 text-emerald-400" />
                  <h3 className="text-sm font-bold text-emerald-300">Dinheiro real</h3>
                  <span className="text-[10px] text-slate-500">· auto-trades resolvidos · {data.window_days}d</span>
                </div>
                {real && real.count > 0 ? (
                  <>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      <StatCard label="Win rate" value={fmtPct(real.win_rate_pct)} sub={`${real.wins}W · ${real.losses}L`} />
                      <StatCard label="Expectancy" value={fmtR(real.expectancy_r)} valueCls={rColor(real.expectancy_r)} sub="média por trade" />
                      <StatCard label="TP1 / TP2 hit" value={`${fmtPct(real.tp1_hit_rate_pct)} / ${fmtPct(real.tp2_hit_rate_pct)}`} sub="taxa de alvos" />
                      <StatCard label="P&L (USD)" value={`$${(real.sum_pnl_usd ?? 0).toFixed(2)}`} valueCls={rColor(real.sum_pnl_usd)} sub={`${real.count} trades`} />
                    </div>
                    <StatusBreakdown by={real.by_status} />
                  </>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">Nenhum auto-trade resolvido na janela ainda.</p>
                )}
              </section>

              {/* ── Shadow (snapshots — amostra maior) ───────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Gauge className="w-4 h-4 text-sky-400" />
                  <h3 className="text-sm font-bold text-sky-300">Shadow (amostra ampla)</h3>
                  <span className="text-[10px] text-slate-500">· recomendações rastreadas · base da calibração</span>
                </div>
                {shadow && shadow.count > 0 ? (
                  <>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      <StatCard label="Win rate" value={fmtPct(shadow.win_rate_pct)} sub={`${shadow.count} resolvidos`} />
                      <StatCard label="Expectancy" value={fmtR(shadow.expectancy_r)} valueCls={rColor(shadow.expectancy_r)} sub="média por setup" />
                      <StatCard label="TP1 hit" value={fmtPct(shadow.tp1_hit_rate_pct)} sub="tocou TP1" />
                      <StatCard label="TP2 hit" value={fmtPct(shadow.tp2_hit_rate_pct)} sub="chegou no TP2" />
                    </div>
                    <StatusBreakdown by={shadow.by_status} />
                  </>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">Sem snapshots resolvidos na janela.</p>
                )}
              </section>

              {/* ── Calibração ───────────────────────────────────────────── */}
              {calib && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldCheck className="w-4 h-4 text-violet-400" />
                    <h3 className="text-sm font-bold text-violet-300">Calibração P(TP1)</h3>
                  </div>
                  <div className="p-3 rounded-lg border border-slate-800 bg-slate-900/40 text-xs text-slate-300 flex flex-wrap items-center gap-x-4 gap-y-1">
                    <span>
                      Status:{' '}
                      {calib.mature
                        ? <span className="text-emerald-300 font-bold">madura ✓</span>
                        : <span className="text-yellow-300 font-bold">aquecendo</span>}
                    </span>
                    <span>Resolvidos: <span className="font-mono text-white">{calib.total_resolved}</span> / {calib.min_sample} mín</span>
                    {calib.win_rate_pct !== null && (
                      <span>P(TP1) global: <span className="font-mono text-white">{fmtPct(calib.win_rate_pct)}</span></span>
                    )}
                  </div>
                  {!calib.mature && (
                    <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                      Enquanto imatura, o gate de P(TP1) não filtra nada (no-op seguro) — começa a morder sozinho ao amadurecer.
                    </p>
                  )}
                </section>
              )}

              {/* ── Gates (skips persistidos) ────────────────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Filter className="w-4 h-4 text-amber-400" />
                  <h3 className="text-sm font-bold text-amber-300">Gates que mais barraram</h3>
                  <span className="text-[10px] text-slate-500">
                    · últimos {gates?.window_days ?? 7}d · {gates?.total_skips ?? 0} skips
                  </span>
                </div>
                {gates && gates.items.length > 0 ? (
                  <div className="flex flex-col gap-1.5">
                    {gates.items.map(g => {
                      const pct = gates.total_skips > 0 ? (g.count / gates.total_skips) * 100 : 0
                      return (
                        <div key={g.gate} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold text-amber-200 capitalize">{gateLabel(g.gate)}</span>
                            <span className="text-[9px] text-slate-600 font-mono">{g.gate}</span>
                            <span className="ml-auto font-mono text-sm font-bold text-white">{g.count}</span>
                          </div>
                          <div className="mt-1 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                            <div className="h-full bg-amber-500/60" style={{ width: `${Math.min(100, pct)}%` }} />
                          </div>
                          {g.last_reason && (
                            <p className="mt-1 text-[10px] text-slate-500 leading-snug truncate" title={g.last_reason}>
                              ex.: {g.last_symbol ? `${g.last_symbol.split('/')[0]} · ` : ''}{g.last_reason}
                            </p>
                          )}
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">
                    Nenhum skip registrado ainda na janela (contadores começam a acumular a partir do deploy desta versão).
                  </p>
                )}
              </section>

              {/* ── Contrafactual: ligar os gates OFF? ───────────────────── */}
              {cf && (cf.quality_edge || cf.regime_sizing || cf.pre_tp1_protect) && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Gauge className="w-4 h-4 text-fuchsia-400" />
                    <h3 className="text-sm font-bold text-fuchsia-300">Ligar os gates desligados?</h3>
                    <span className="text-[10px] text-slate-500">· evidência histórica · {cf.window_days ?? data.window_days}d</span>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <CfRow name="conviction_up" label="Subir o teto de size por convicção (>1.0×)" block={cf.conviction_up} />
                    <CfRow name="edge_sizing" label="Sizing por edge (A+/funding/padrão/MTF)" block={cf.edge_sizing} />
                    <CfRow name="quality_edge" label="Gate de score marginal sem edge" block={cf.quality_edge} />
                    <CfRow name="regime_sizing" label="Reduzir size de alt-long em regime adverso" block={cf.regime_sizing} />
                    <CfRow name="pre_tp1_protect" label="Proteção pré-TP1 (trava lucro parcial)" block={cf.pre_tp1_protect} />
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Mede sobre o histórico resolvido o que cada gate DESLIGADO teria feito — transforma "ligar ou não" em evidência.
                  </p>
                </section>
              )}

              {/* ── Oportunidades detectadas (evidência, ainda não executadas) ── */}
              {(pyr || hedge) && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Target className="w-4 h-4 text-cyan-400" />
                    <h3 className="text-sm font-bold text-cyan-300">Oportunidades detectadas</h3>
                    <span className="text-[10px] text-slate-500">· evidência · execução ainda não implementada</span>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {pyr?.verdict && (
                      <div className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                        <span className="text-xs font-bold text-cyan-200">Pyramiding (adicionar em trade vencedor)</span>
                        <p className="mt-1 text-[10px] text-slate-400 leading-snug">{pyr.verdict}</p>
                      </div>
                    )}
                    {hedge?.verdict && (
                      <div className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                        <span className="text-xs font-bold text-cyan-200">Hedge em regime adverso</span>
                        <p className="mt-1 text-[10px] text-slate-400 leading-snug">{hedge.verdict}</p>
                      </div>
                    )}
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Estas duas ADICIONAM risco (ordens novas) — a execução fica pra um passo com backtest dedicado. Aqui só a evidência.
                  </p>
                </section>
              )}

              {/* ── P05.1 Qualidade da evidência ─────────────────────────── */}
              {p05?.enabled && eq && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldCheck className="w-4 h-4 text-teal-400" />
                    <h3 className="text-sm font-bold text-teal-300">Qualidade da evidência</h3>
                    <span className="text-[10px] text-slate-500">· dá pra confiar no que está sendo medido?</span>
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    <StatCard label="Amostra" value={`${eq.shadow_resolved ?? 0}`}
                      sub={`mín. ${eq.min_offline_required ?? 60} p/ avaliar`} />
                    <StatCard label="Maturidade" value={RELIABILITY_LABEL[eq.maturity ?? ''] ?? '–'}
                      valueCls={eq.ready_for_candidates ? 'text-emerald-300' : 'text-yellow-300'}
                      sub={eq.ready_for_candidates ? 'pronta' : 'ainda juntando dados'} />
                    <StatCard label="Dados utilizáveis" value={`${eq.features_usable?.length ?? 0}`}
                      sub={`${eq.features_below_coverage?.length ?? 0} com cobertura baixa`} />
                    <StatCard label="Descartados" value={`${eq.excluded_total ?? 0}`}
                      sub="sem resultado válido" />
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Só entra na conta o que tem resultado fechado e número válido. O que falta dado fica de fora — e aparece aqui.
                  </p>
                </section>
              )}

              {/* ── P05.2 Onde ganha / onde perde ────────────────────────── */}
              {p05?.enabled && p05.segments && Object.keys(p05.segments).length > 0 && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Target className="w-4 h-4 text-lime-400" />
                    <h3 className="text-sm font-bold text-lime-300">Onde ganha / onde perde</h3>
                    <span className="text-[10px] text-slate-500">· por grupo · {data.window_days}d</span>
                  </div>
                  <div className="flex flex-col gap-2">
                    {Object.entries(p05.segments).map(([axis, block]) => (
                      <div key={axis} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                        <div className="text-[11px] font-bold text-slate-300 mb-1">
                          {SEGMENT_AXIS_LABEL[axis] ?? axis}
                        </div>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-1">
                          <div>
                            <div className="text-[9px] uppercase text-emerald-400/80 mb-0.5">melhores</div>
                            {(block.best ?? []).map(s => <SegRow key={`b-${s.key}`} s={s} />)}
                          </div>
                          <div>
                            <div className="text-[9px] uppercase text-red-400/80 mb-0.5">piores</div>
                            {(block.worst ?? []).map(s => <SegRow key={`w-${s.key}`} s={s} />)}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Grupo com poucos casos não é prova — o rótulo de confiança mostra o quanto dá pra levar a sério.
                  </p>
                </section>
              )}

              {/* ── P05.3 Champion × Challenger ──────────────────────────── */}
              {p05?.enabled && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Gauge className="w-4 h-4 text-indigo-400" />
                    <h3 className="text-sm font-bold text-indigo-300">Champion × Challenger</h3>
                    <span className="text-[10px] text-slate-500">· teste lado a lado, sem operar</span>
                  </div>
                  {exp && sm ? (
                    <>
                      <div className="p-2 rounded-lg border border-slate-800 bg-slate-900/40 mb-2 text-[11px] text-slate-300">
                        <span className="font-bold text-indigo-200">
                          {exp.objective === 'LOSS_REDUCTION' ? 'Reduzir perdas' : 'Operar mais'}
                        </span>
                        <span className="ml-2 text-slate-500">
                          ajuste: {describeConfig(exp.candidate_config)}
                        </span>
                        <span className="ml-auto float-right font-mono text-slate-400">{exp.status}</span>
                        {isContextual(exp.candidate_config) && (
                          <div className="mt-1 text-[10px] text-amber-300/90">
                            Regra por contexto — <strong>somente análise</strong>: não é aplicada
                            pelo bot e não pode ser ativada por aqui.
                          </div>
                        )}
                      </div>
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                        <StatCard label="Operações" value={`${sm.champion?.count ?? 0} → ${sm.candidate?.count ?? 0}`}
                          sub={`+${sm.added_ops ?? 0} / −${sm.avoided_ops ?? 0}`} />
                        <StatCard label="Expectancy" value={fmtR(sm.candidate?.expectancy_r)}
                          valueCls={rColor(sm.candidate?.expectancy_r)}
                          sub={`atual ${fmtR(sm.champion?.expectancy_r)}`} />
                        <StatCard label="Profit factor" value={sm.candidate?.profit_factor?.toFixed(2) ?? '–'}
                          sub={`atual ${sm.champion?.profit_factor?.toFixed(2) ?? '–'}`} />
                        <StatCard label="Queda máx." value={fmtR(sm.candidate?.max_drawdown_r)}
                          sub={`atual ${fmtR(sm.champion?.max_drawdown_r)}`} />
                      </div>
                      <div className="mt-1.5 p-2 rounded-lg border border-slate-800 bg-slate-900/40 text-[10px] text-slate-400 leading-snug">
                        Amostra: <span className="font-mono text-slate-300">{sm.challenger_resolved ?? 0}</span> resolvidos ·{' '}
                        <span className="font-mono text-slate-300">{sm.observed_days ?? 0}</span> dias ·{' '}
                        cobertura <span className="font-mono text-slate-300">{fmtPct(sm.coverage_pct)}</span>
                        {exp.decision?.detail && <div className="mt-0.5">Situação: {exp.decision.detail}</div>}
                      </div>
                    </>
                  ) : (
                    <p className="text-xs text-slate-500 italic px-1">
                      Nenhum teste em andamento. Quando houver, o comportamento atual e o alternativo aparecem lado a lado aqui.
                    </p>
                  )}
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    O alternativo só é <strong className="text-slate-400">observado</strong>: não muda nota, não bloqueia recomendação e não abre operação.
                  </p>
                </section>
              )}

              {/* ── P05.4 Bloqueios P04 ──────────────────────────────────── */}
              {p05?.enabled && p05.gate_events && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Filter className="w-4 h-4 text-orange-400" />
                    <h3 className="text-sm font-bold text-orange-300">Bloqueios de segurança (P04)</h3>
                    <span className="text-[10px] text-slate-500">· travas que impediram entrada</span>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {Object.entries(p05.gate_events.by_phase ?? {}).map(([phase, info]) => (
                      <div key={phase} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40 flex items-center gap-2">
                        <span className="text-xs font-bold text-orange-200">{phase.replace(/_/g, ' ')}</span>
                        <span className="ml-auto font-mono text-sm font-bold text-white">
                          {info.events === null || info.events === undefined ? 'n/d' : info.events}
                        </span>
                        {info.reason && (
                          <span className="w-full text-[10px] text-slate-500 leading-snug">{info.reason}</span>
                        )}
                      </div>
                    ))}
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    São contagens de <strong className="text-slate-400">eventos</strong> de bloqueio, não de oportunidades únicas —
                    e um bloqueio não significa dinheiro deixado na mesa: não dá pra saber o resultado de algo que não aconteceu.
                  </p>
                </section>
              )}

              {/* ── P05.1T Qualidade da telemetria (somente observação) ──── */}
              {p05?.enabled && (p05.telemetry || p05.mae_mfe) && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <Gauge className="w-4 h-4 text-cyan-400" />
                    <h3 className="text-sm font-bold text-cyan-300">Qualidade da telemetria</h3>
                    <span className="text-[10px] text-slate-500">· o que já dá pra medir</span>
                  </div>

                  {/* Cobertura dos contextos — o que decide é o TREINO */}
                  {p05.telemetry?.context_coverage?.axes && (
                    <div className="flex flex-col gap-1.5 mb-2">
                      {Object.entries(p05.telemetry.context_coverage.axes).map(([axis, a]) => {
                        const train = a.coverage_train_pct ?? 0
                        const min = p05.telemetry?.context_coverage?.min_required_pct ?? 80
                        return (
                          <div key={axis} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-xs font-bold text-slate-200">
                                {AXIS_LABEL[axis] ?? axis}
                              </span>
                              <span className="text-[10px] text-slate-400">
                                treino da janela de {p05.telemetry?.context_coverage?.window_days ?? 120} dias:{' '}
                                <span className="font-mono text-slate-200">{fmtPct(a.coverage_train_pct)}</span>
                                {' '}de {min}%
                              </span>
                              <span className={`ml-auto text-[10px] font-bold ${
                                a.status === 'USABLE' ? 'text-emerald-300' : 'text-yellow-300'}`}>
                                {TELEMETRY_LABEL[a.status ?? ''] ?? a.status}
                              </span>
                            </div>
                            <div className="mt-1 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                              <div className={`h-full ${train >= min ? 'bg-emerald-500/60' : 'bg-yellow-500/60'}`}
                                style={{ width: `${Math.min(100, (train / min) * 100)}%` }} />
                            </div>
                            <div className="mt-0.5 text-[9px] text-slate-500">
                              geral {fmtPct(a.coverage_global_pct)} · {a.present ?? 0} com dado · {a.missing ?? 0} sem
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}

                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    <StatCard
                      label="Excursão (MAE/MFE)"
                      value={TELEMETRY_LABEL[p05.mae_mfe?.status ?? ''] ?? '–'}
                      valueCls={p05.mae_mfe?.status === 'USABLE' ? 'text-emerald-300' : 'text-yellow-300'}
                      sub={`${p05.mae_mfe?.observed ?? 0} de ${p05.mae_mfe?.min_observed ?? 30} mín`} />
                    <StatCard
                      label="Cobertura MAE/MFE"
                      value={fmtPct(p05.mae_mfe?.coverage_pct)}
                      sub={`${p05.mae_mfe?.missing ?? 0} sem registro`} />
                    <StatCard
                      label="Deslize de preço"
                      value={p05.telemetry?.slippage?.mean !== null && p05.telemetry?.slippage?.mean !== undefined
                        ? `${p05.telemetry.slippage.mean.toFixed(3)}%` : '–'}
                      sub={`cobertura ${fmtPct(p05.telemetry?.slippage?.coverage_pct)}`} />
                    <StatCard
                      label="Histórico guardado"
                      value={`${p05.telemetry?.retention?.observed_retention_days ?? '–'} d`}
                      sub="evidência preservada" />
                  </div>

                  {p05.mae_mfe?.status === 'USABLE' && (
                    <div className="mt-1.5 p-2 rounded-lg border border-slate-800 bg-slate-900/40 text-[10px] text-slate-400 leading-snug">
                      Quanto o preço costuma andar contra antes de dar certo (mediana):{' '}
                      <span className="font-mono text-red-300">{p05.mae_mfe.mae_r?.median ?? '–'}R</span>
                      {' '}· e a favor:{' '}
                      <span className="font-mono text-emerald-300">{p05.mae_mfe.mfe_r?.median ?? '–'}R</span>
                    </div>
                  )}

                  {p05.telemetry?.latency?.status === 'UNAVAILABLE' && (
                    <div className="mt-1.5 text-[10px] text-slate-500 leading-snug px-1">
                      Tempo de execução: <strong className="text-slate-400">não medido</strong> — não há
                      marcação de horário confiável do pedido até a confirmação da corretora.
                    </div>
                  )}

                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Medido em <strong className="text-slate-400">velas de 5 minutos</strong> do setup observado, não
                    da operação real — a ordem dos preços dentro da vela é desconhecida. Serve só para
                    acompanhar a qualidade dos dados; não é sugestão de mudança.
                  </p>
                </section>
              )}

              {/* ── P05.1R Prontidão para nova avaliação (somente leitura) ── */}
              {readiness?.experiments && readiness.experiments.length > 0 && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldCheck className="w-4 h-4 text-sky-400" />
                    <h3 className="text-sm font-bold text-sky-300">Prontidão para nova avaliação</h3>
                    <span className="ml-auto text-[9px] px-1.5 py-0.5 rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-300">
                      🔒 holdout protegido
                    </span>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    {readiness.experiments.map(e => {
                      const cls = READINESS_COLOR[e.readiness ?? 'UNKNOWN'] ?? 'text-slate-400'
                      const pv = e.prospective?.prospective_validation_affected ?? 0
                      const pt = e.prospective?.prospective_test_affected ?? 0
                      const min = e.minimum_affected ?? 20
                      return (
                        <div key={e.experiment_id} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-xs font-bold text-slate-200">
                              {AXIS_LABEL[e.axis ?? ''] ?? e.axis} = {e.value}
                            </span>
                            <span className="text-[9px] font-mono text-slate-500">
                              {e.action === 'BLOCK' ? 'não operar' : `nota −${Math.abs(e.score_delta ?? 0)}`}
                            </span>
                            <span className={`ml-auto text-[10px] font-bold ${cls}`}>
                              {READINESS_LABEL[e.readiness ?? 'UNKNOWN'] ?? e.readiness}
                            </span>
                          </div>
                          <div className="mt-1 text-[10px] text-slate-400 leading-snug">
                            Última conclusão: {e.failure_reason || '–'}
                          </div>
                          <div className="mt-1 grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-0.5 text-[10px]">
                            <span className="text-slate-400">
                              afetadas hoje: <span className="font-mono text-slate-200">{pv} val / {pt} teste</span>
                            </span>
                            <span className="text-slate-400">
                              mínimo: <span className="font-mono text-slate-200">{min}</span>
                            </span>
                            <span className="text-slate-400">
                              falta: <span className="font-mono text-slate-200">{e.missing_to_minimum ?? '–'}</span>
                            </span>
                            <span className="text-slate-400">
                              cobertura: <span className="font-mono text-slate-200">{fmtPct(e.current_window?.context_coverage_pct)}</span>
                            </span>
                            <span className="text-slate-400">
                              ritmo: <span className="font-mono text-slate-200">{e.daily_rate !== null && e.daily_rate !== undefined ? `${e.daily_rate.toFixed(2)}/dia` : '–'}</span>
                            </span>
                            <span className="text-slate-400">
                              previsão: <span className="font-mono text-slate-200">{e.eta_days !== null && e.eta_days !== undefined ? `~${e.eta_days}d` : '–'}</span>
                            </span>
                          </div>
                          {e.eta_reason && (
                            <div className="mt-0.5 text-[9px] text-slate-500 leading-snug">{e.eta_reason}</div>
                          )}
                        </div>
                      )
                    })}
                  </div>

                  {readiness.retention && (
                    <div className="mt-1.5 p-2 rounded-lg border border-slate-800 bg-slate-900/40 text-[10px] text-slate-400 leading-snug">
                      Histórico disponível:{' '}
                      <span className="font-mono text-slate-200">
                        {readiness.retention.observed_retention_days ?? '–'} dias
                      </span>
                      {readiness.retention.history_note && <> · {readiness.retention.history_note}</>}
                      {readiness.retention.retention_warning && (
                        <div className="mt-0.5 text-amber-300">⚠ {readiness.retention.retention_warning}</div>
                      )}
                    </div>
                  )}

                  <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                    Isto mede apenas se já existe <strong className="text-slate-400">amostra suficiente para reavaliar</strong> —
                    não diz que a regra funciona, nem que está pronta para ser usada. Os resultados guardados
                    para o teste final continuam intocados.
                  </p>
                </section>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">Dinheiro real</strong> = auto-trades executados (amostra pequena, alta confiança).{' '}
          <strong className="text-slate-400">Shadow</strong> = recomendações rastreadas (amostra ampla, mesma base da calibração).{' '}
          <strong className="text-slate-400">Gates</strong> = motivos de veto persistidos — sobrevivem a redeploy.
        </div>
      </div>
    </div>
  )
}

// Regra contextual do P05.1 — só análise, nunca aplicada pelo bot.
interface ContextRule {
  axis?: string
  value?: string
  action?: string
  score_delta?: number
}

const AXIS_LABEL: Record<string, string> = {
  regime: 'regime de mercado',
  entry_zone_type: 'tipo de entrada',
}

function isContextual(config?: Record<string, unknown> | null): boolean {
  return !!config && Object.prototype.hasOwnProperty.call(config, 'CONTEXT_RULE')
}

/** Descreve a configuração em texto legível — nunca renderiza [object Object]. */
function describeConfig(config?: Record<string, unknown> | null): string {
  const entries = Object.entries(config ?? {})
  if (entries.length === 0) return '–'
  return entries
    .map(([key, raw]) => {
      if (key === 'CONTEXT_RULE' && raw && typeof raw === 'object') {
        const r = raw as ContextRule
        const axis = AXIS_LABEL[r.axis ?? ''] ?? r.axis ?? '?'
        const acao = r.action === 'BLOCK'
          ? 'não operar'
          : `exigir ${Math.abs(r.score_delta ?? 0)} ponto(s) a menos de nota`
        return `quando ${axis} = ${r.value ?? '?'} → ${acao}`
      }
      if (raw && typeof raw === 'object') {
        return `${key} → ${Object.entries(raw as Record<string, unknown>)
          .map(([k2, v2]) => `${k2}=${String(v2)}`).join(' · ')}`
      }
      return `${key} → ${String(raw)}`
    })
    .join(', ')
}

function SegRow({ s }: { s: P05Segment }) {
  return (
    <div className="flex items-center gap-1.5 text-[10px] py-0.5">
      <span className="text-slate-300 truncate max-w-[7rem]" title={s.key}>{s.key}</span>
      <span className={`font-mono ${rColor(s.avg_r)}`}>{fmtR(s.avg_r)}</span>
      <span className="text-slate-600">n={s.count}</span>
      <span className="ml-auto text-slate-500 truncate">{RELIABILITY_LABEL[s.reliability] ?? s.reliability}</span>
    </div>
  )
}

function StatCard({ label, value, sub, valueCls }: { label: string; value: string; sub?: string; valueCls?: string }) {
  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
      <div className="text-[10px] text-slate-500 uppercase">{label}</div>
      <div className={`text-lg font-bold font-mono ${valueCls ?? 'text-white'}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-600 mt-0.5">{sub}</div>}
    </div>
  )
}

function CfRow({ name, label, block }: { name: string; label: string; block?: CfBlock }) {
  if (!block) return null
  const on = block.enabled_now === true
  return (
    <div className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
      <div className="flex items-center gap-2">
        <span className="text-xs font-bold text-fuchsia-200">{label}</span>
        <span className="text-[9px] text-slate-600 font-mono">{name}</span>
        <span className={`ml-auto px-1.5 py-0.5 rounded text-[9px] font-bold border ${
          on
            ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
            : 'bg-slate-800 border-slate-700 text-slate-400'
        }`}>{on ? 'LIGADO' : 'DESLIGADO'}</span>
      </div>
      {block.verdict && (
        <p className="mt-1 text-[10px] text-slate-400 leading-snug">{block.verdict}</p>
      )}
    </div>
  )
}

function StatusBreakdown({ by }: { by: Record<string, number> }) {
  const entries = Object.entries(by || {}).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {entries.map(([status, n]) => (
        <span key={status} className="px-2 py-0.5 rounded text-[10px] font-mono border border-slate-700 bg-slate-800/60 text-slate-300">
          {status} <span className="text-white font-bold">{n}</span>
        </span>
      ))}
    </div>
  )
}
