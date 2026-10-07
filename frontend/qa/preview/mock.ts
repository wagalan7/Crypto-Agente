// ─── Isolamento de QA (Lote 04) ──────────────────────────────────────────────
// Instala ANTES de montar a app. Toda requisição é interceptada:
//  • endpoints conhecidos → FIXTURE SINTÉTICA, explicitamente rotulada;
//  • qualquer outro host (PRD, DEV, Binance, Bybit, Telegram…) → BLOQUEADO e
//    CONTADO; a promessa é rejeitada (nunca cai para produção);
//  • POST/PATCH → INTERCEPTADOS e contados, jamais enviados;
//  • WebSocket → substituído por um stub que não abre conexão.
// Nenhuma ENV de produção é lida: o host base é um sentinela local.

export interface QaCall { method: string; url: string; blocked: boolean; mutating: boolean }

declare global {
  interface Window {
    __QA__: {
      calls: QaCall[]
      blocked: QaCall[]
      mutations: QaCall[]
      sockets: string[]
      scenario: string
      summary(): Record<string, unknown>
    }
  }
}

const ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
const MUTATING = ['POST', 'PUT', 'PATCH', 'DELETE']

const calls: QaCall[] = []
const blocked: QaCall[] = []
const mutations: QaCall[] = []
const sockets: string[] = []

/** Cenário de QA via `?cenario=`. */
export type Scenario = 'sem-bloqueio' | 'p03-bloqueado' | 'sem-confirmacao' | 'pausa-manual'
const params = new URLSearchParams(window.location.search)
const scenario = (params.get('cenario') ?? 'sem-bloqueio') as Scenario

// ─── Fixtures SINTÉTICAS (nunca valores de produção) ─────────────────────────
const SYN = 'SINTÉTICO-QA'

const riskByScenario: Record<Scenario, unknown> = {
  'sem-bloqueio': {
    enabled: true, trading_paused: false, pause_reason: null, pause_manual: false,
    paused_at: null, daily_dd_pct: -0.42, weekly_dd_pct: -1.1,
    daily_trades: 3, weekly_trades: 11, daily_limit_pct: -3, weekly_limit_pct: -6,
    current_day_utc: '2026-10-07', current_week_utc: '2026-W41', updated_at: '2026-10-07T12:00:00Z',
  },
  'p03-bloqueado': {
    enabled: true, trading_paused: true,
    pause_reason: 'P03-QUARANTINE:UNKNOWN_NO_SL incident=42',
    pause_manual: false, paused_at: '2026-10-07T11:30:00Z',
    daily_dd_pct: -0.9, weekly_dd_pct: -2.2,
    daily_trades: 1, weekly_trades: 7, daily_limit_pct: -3, weekly_limit_pct: -6,
    current_day_utc: '2026-10-07', current_week_utc: '2026-W41', updated_at: '2026-10-07T12:00:00Z',
  },
  'pausa-manual': {
    enabled: true, trading_paused: true, pause_reason: 'kill switch manual',
    pause_manual: true, paused_at: '2026-10-07T10:00:00Z',
    daily_dd_pct: 0.3, weekly_dd_pct: -0.5,
    daily_trades: 2, weekly_trades: 9, daily_limit_pct: -3, weekly_limit_pct: -6,
    current_day_utc: '2026-10-07', current_week_utc: '2026-W41', updated_at: '2026-10-07T12:00:00Z',
  },
  // Risco e incidentes FALHAM: a tela precisa dizer "sem confirmação".
  'sem-confirmacao': null,
}

const p03ByScenario: Record<Scenario, unknown> = {
  'sem-bloqueio': {
    ok: true, reconciler_running: true, last_reconciliation_at: '2026-10-07T12:00:00Z',
    quarantine_active: false, open_total: 0, retry_pending: 0, manual_required: 0,
    protected: 4, flat: 9, items: [], manual_items: [],
  },
  'p03-bloqueado': {
    ok: true, reconciler_running: true, last_reconciliation_at: '2026-10-07T11:58:00Z',
    quarantine_active: true, open_total: 2, retry_pending: 1, manual_required: 1,
    protected: 1, flat: 3,
    items: [
      { incident_id: 42, incident_key: `${SYN}-42`, symbol: 'SYN/USDT:USDT', side: 'long',
        kind: 'ENTRY_UNKNOWN', state: 'MANUAL_REQUIRED', qty_known: 0.5, attempts: 3,
        last_error: 'stop não confirmado após emissão (fixture sintética)',
        manual_reason: 'SL_NOT_CONFIRMED', next_retry_at: null, updated_at: '2026-10-07T11:58:00Z' },
      { incident_id: 43, incident_key: `${SYN}-43`, symbol: 'SYN2/USDT:USDT', side: 'short',
        kind: 'PROTECTION_UNKNOWN', state: 'RETRY_PENDING', qty_known: null, attempts: 1,
        last_error: null, manual_reason: null,
        next_retry_at: '2026-10-07T12:05:00Z', updated_at: '2026-10-07T11:57:00Z' },
    ],
    manual_items: [{ incident_id: 42, state: 'MANUAL_REQUIRED', symbol: 'SYN/USDT:USDT' }],
  },
  'pausa-manual': {
    ok: true, reconciler_running: true, last_reconciliation_at: '2026-10-07T12:00:00Z',
    quarantine_active: false, open_total: 0, retry_pending: 0, manual_required: 0,
    items: [], manual_items: [],
  },
  'sem-confirmacao': null,
}

const dailyPnl = {
  summary: {
    total_trades: 4, wins: 3, losses: 1, win_rate_pct: 75, total_r: 1.86,
    total_pct_banca: 0.93, still_open: 1,
  },
  trades: [], open_trades: [],
}

const realTrades = {
  count: 2, days: 30,
  trades: [
    { id: 1, symbol: 'SYN/USDT:USDT', side: 'long', source: 'auto', recommendation_id: 9,
      qty: 1, qty_initial: 1, leverage: 5, notional_usd: 100, entry_price: 100,
      opened_at: '2026-10-07T09:00:00Z', planned_stop: 95, planned_tp1: 105, planned_tp2: 110,
      exit_price: null, closed_at: null, status: 'open', phase: 'post_tp1',
      sl_current_price: 100, realized_r: 0.8, pnl_usd: null, pnl_pct: 1.2,
      entry_slippage_pct: null, notes: SYN },
    { id: 2, symbol: 'SYN2/USDT:USDT', side: 'short', source: 'manual', recommendation_id: null,
      qty: 2, qty_initial: 2, leverage: 3, notional_usd: 200, entry_price: 50,
      opened_at: '2026-10-07T08:00:00Z', planned_stop: 53, planned_tp1: 47, planned_tp2: 44,
      exit_price: null, closed_at: null, status: 'open', phase: null,
      sl_current_price: 53, realized_r: null, pnl_usd: null, pnl_pct: null,
      entry_slippage_pct: null, notes: SYN },
  ],
}

const recommendations = {
  count: 2,
  recommendations: [
    { tier: 'A+', score: 82, symbol: 'SYN/USDT:USDT', timeframe: '4h', direction: 'long',
      confidence: 0.82, risk_reward: 2.4, entry: 100, stop_loss: 95, tp2: 110,
      summary: `Setup sintético de QA (${SYN})`, warnings: [], leverage: 5, risk_pct: 1,
      margin_pct: 10, stop_distance_pct: 5, prob_tp1: 0.61, edge_tags: ['pullback'],
      bot_verdict: { ok: true, blocked_by: null, reason: null } },
    { tier: 'A', score: 71, symbol: 'SYN2/USDT:USDT', timeframe: '1h', direction: 'short',
      confidence: 0.71, risk_reward: 1.8, entry: 50, stop_loss: 53, tp2: 44,
      summary: `Setup sintético de QA (${SYN})`, warnings: [], leverage: 3, risk_pct: 1,
      margin_pct: 10, stop_distance_pct: 6, prob_tp1: null, edge_tags: [],
      bot_verdict: { ok: false, blocked_by: 'RR_GATE', reason: 'R:R abaixo do mínimo' } },
  ],
}

const paperSummary = {
  enabled: true, mode: 'paper', days: 30,
  equity: {
    final_pnl_pct: 4.2, trades_total: 36,
    curve: Array.from({ length: 12 }, (_, i) => ({
      date: `2026-09-${String(i + 1).padStart(2, '0')}`,
      cumulative_pct: Number((Math.sin(i / 2) * 2 + i * 0.35).toFixed(2)),
    })),
  },
  tier_stats: {
    'A+': { n: 12, wins: 9, losses: 3, wr_pct: 75, avg_r: 0.6, expectancy_r: 0.5, pnl_pct: 3.1 },
    A: { n: 18, wins: 11, losses: 7, wr_pct: 61, avg_r: 0.2, expectancy_r: 0.15, pnl_pct: 1.4 },
  },
}

const health = {
  enabled: true, status: 'healthy', last_alive_ts: '2026-10-07T12:00:00Z',
  gap_seconds: 18, gap_alert_threshold: 300, last_source: `scanner-${SYN}`, tick_count: 1284,
}

const ROUTES: { test: RegExp; body: () => unknown }[] = [
  { test: /\/api\/risk\/status/, body: () => riskByScenario[scenario] },
  { test: /\/api\/execution-incidents\/status/, body: () => p03ByScenario[scenario] },
  { test: /\/api\/risk\/events/, body: () => ({ events: [] }) },
  { test: /\/api\/daily-pnl/, body: () => dailyPnl },
  { test: /\/api\/real-trades\/summary/, body: () => ({ enabled: true, equity: { trades_total: 0 }, tier_stats: {} }) },
  { test: /\/api\/real-trades/, body: () => realTrades },
  { test: /\/api\/recommendations/, body: () => recommendations },
  { test: /\/api\/paper\/summary/, body: () => paperSummary },
  { test: /\/api\/admin\/health/, body: () => health },
  { test: /\/api\/regime-status/, body: () => ({ regime: 'NORMAL', btc_24h_pct: 0.4, btc_dominance: 57.2, reasons: [] }) },
  { test: /\/api\/macro/, body: () => ({ btc_direction: 'alta', btc_rsi: 55, btc_adx: 22, btc_dominance: 57.2, market_data: {}, context_text: SYN }) },
  { test: /\/api\/symbols/, body: () => ({ symbols: ['SYN/USDT:USDT', 'SYN2/USDT:USDT'], count: 2 }) },
  { test: /\/api\/tickers/, body: () => ({ tickers: [] }) },
  { test: /\/api\/calibration/, body: () => ({ enabled: false }) },
  { test: /\/api\//, body: () => ({ enabled: false, qa_fixture: SYN }) },
]

function record(entry: QaCall) {
  calls.push(entry)
  if (entry.blocked) blocked.push(entry)
  if (entry.mutating) mutations.push(entry)
}

export function installQaIsolation(): void {
  const originalFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    const mutating = MUTATING.includes(method)
    let host = ''
    try { host = new URL(url, window.location.origin).hostname } catch { host = '' }
    const external = host !== '' && !ALLOWED_HOSTS.includes(host)

    // Assets locais do servidor de preview continuam normais.
    if (!external && !/\/api\//.test(url) && !mutating) {
      record({ method, url, blocked: false, mutating: false })
      return originalFetch(input as RequestInfo, init)
    }
    if (external) {
      record({ method, url, blocked: true, mutating })
      throw new Error(`[QA] requisição externa BLOQUEADA: ${method} ${url}`)
    }
    if (mutating) {
      // Ação mutável existente: contada e NUNCA enviada.
      record({ method, url, blocked: true, mutating: true })
      throw new Error(`[QA] mutação interceptada (não enviada): ${method} ${url}`)
    }
    const route = ROUTES.find(r => r.test.test(url))
    const body = route ? route.body() : null
    record({ method, url, blocked: false, mutating: false })
    if (body === null) {
      // Cenário "sem confirmação": a fonte responde erro de verdade.
      return new Response(JSON.stringify({ detail: `[QA] fonte indisponível (${scenario})` }),
        { status: 503, headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }

  class StubSocket {
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSING = 2
    static readonly CLOSED = 3
    readyState = 3
    onmessage: ((e: MessageEvent) => void) | null = null
    onopen: (() => void) | null = null
    onerror: (() => void) | null = null
    onclose: (() => void) | null = null
    constructor(url: string) { sockets.push(url) }
    send() { /* nada sai */ }
    close() { /* nada a fechar */ }
    addEventListener() { /* noop */ }
    removeEventListener() { /* noop */ }
  }
  ;(window as unknown as { WebSocket: unknown }).WebSocket = StubSocket

  // Service worker e notificações ficam fora do QA.
  if ('serviceWorker' in navigator) {
    Object.defineProperty(navigator, 'serviceWorker', { value: undefined, configurable: true })
  }

  window.__QA__ = {
    calls, blocked, mutations, sockets, scenario,
    summary: () => ({
      scenario,
      total: calls.length,
      externalBlocked: blocked.filter(c => !c.mutating).length,
      mutationsIntercepted: mutations.length,
      sockets: sockets.length,
      apiPaths: Array.from(new Set(calls.filter(c => /\/api\//.test(c.url))
        .map(c => c.url.replace(/^https?:\/\/[^/]+/, '').split('?')[0]))).sort(),
      p03Calls: calls.filter(c => /execution-incidents/.test(c.url)).length,
    }),
  }
  // eslint-disable-next-line no-console
  console.info(`[QA] isolamento instalado · cenário=${scenario} · fixtures ${SYN}`)
}
