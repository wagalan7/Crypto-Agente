// ─── Estado operacional: função PURA de apresentação (Lote 04) ───────────────
// Um único lugar decide o que a tela diz sobre "o bot pode entrar agora?".
// Home, header (badge) e Sistema consomem ESTA função — não duplicam a regra.
//
// Contrato desta camada:
//  • entra FATO + QUALIDADE da leitura + INSTANTE; sai rótulo/motivo/lacuna;
//  • nenhuma consulta, nenhum efeito, nenhuma mutação, nenhum relógio interno
//    (o chamador passa `nowMs`, então o frescor nunca se renova ao renderizar);
//  • ausência NUNCA vira zero, false ou verde;
//  • `trading_paused === false` sozinho NÃO prova autoridade de entrada.

export type ReadingState = 'LOADING' | 'OK' | 'ERROR'

/** Leitura de UMA fonte: valor conhecido + qualidade + instante do último OK. */
export interface SourceReading<T> {
  state: ReadingState
  /** Último valor conhecido. Pode ser de uma leitura ANTERIOR (ver `lastOkAtMs`). */
  value: T | null
  /** Instante (ms epoch) da última leitura BEM-SUCEDIDA desta fonte. */
  lastOkAtMs: number | null
  /** Motivo técnico do erro atual, quando houver. Nunca é exibido cru. */
  errorReason?: string | null
}

export function emptyReading<T>(): SourceReading<T> {
  return { state: 'LOADING', value: null, lastOkAtMs: null, errorReason: null }
}

export type OperationalLevel =
  /** Primeira leitura ainda em andamento. */
  | 'LOADING'
  /** Sem confirmação atual (erro, ausência ou payload incoerente). */
  | 'UNAVAILABLE'
  /** Havia leitura boa, mas ela é antiga demais para confirmar agora. */
  | 'STALE'
  /** Bloqueio confirmado: pausa de risco, incidente P03 ou quarentena. */
  | 'BLOCKED'
  /** Risco e P03 confirmam que NÃO há bloqueio — sem afirmar autorização. */
  | 'NO_BLOCK_CONFIRMED'
  /** Autoridade de entrada confirmada EXPLICITAMENTE pelo backend. */
  | 'AVAILABLE'

export type Tone = 'neutral' | 'info' | 'ok' | 'warn' | 'danger'

export interface RiskFacts {
  enabled?: unknown
  trading_paused?: unknown
  pause_reason?: unknown
  pause_manual?: unknown
  paused_at?: unknown
  daily_dd_pct?: unknown
  weekly_dd_pct?: unknown
  daily_limit_pct?: unknown
  weekly_limit_pct?: unknown
}

/** Campos do GET público `/api/execution-incidents/status` (somente leitura). */
export interface P03Facts {
  ok?: unknown
  quarantine_active?: unknown
  open_total?: unknown
  manual_required?: unknown
  retry_pending?: unknown
  last_reconciliation_at?: unknown
  reconciler_running?: unknown
  error?: unknown
  items?: unknown
  manual_items?: unknown
}

/**
 * Fato de AUTORIDADE DE ENTRADA, se algum dia o backend publicar um.
 * Hoje nenhum endpoint do contrato publica isso: a Home não inventa o agregado.
 */
export interface EntryAuthorityFacts {
  confirmed?: unknown
  source?: unknown
}

/**
 * Escopo DECLARADO do consumidor.
 *  • `FULL` — a tela afirma sobre risco E incidentes (Home, Sistema);
 *  • `RISK_ONLY` — o controle fala só de risco (selo do header, que por
 *    contrato não faz leitura própria de incidentes). O P03 entra em
 *    `missing`, então o selo nunca sugere autorização agregada.
 */
export type OperationalScope = 'FULL' | 'RISK_ONLY'

export interface OperationalInput {
  risk: SourceReading<RiskFacts>
  p03: SourceReading<P03Facts>
  scope?: OperationalScope
  entryAuthority?: SourceReading<EntryAuthorityFacts> | null
  /** Relógio do chamador. Obrigatório: a função não lê o relógio da tela. */
  nowMs: number
  /** Tolerância de apresentação. Default = 3× a cadência de 20s da Home. */
  staleAfterMs?: number
}

export interface OperationalState {
  level: OperationalLevel
  tone: Tone
  /** Rótulo curto para strip/badge. */
  title: string
  /** Uma frase explicando o nível. */
  detail: string
  /** Causas humanas conhecidas (vocabulário controlado). */
  reasons: string[]
  /** Detalhe técnico saneado, para revelação opcional. */
  technical: string[]
  /** O que NÃO foi confirmado nesta leitura. */
  missing: string[]
  /** Instante da leitura de risco mais recente bem-sucedida. */
  lastKnownAtMs: number | null
  /** Verdadeiro quando o dado exibido é de uma leitura antiga. */
  stale: boolean
  /** Verdadeiro quando existe bloqueio confirmado (risco, P03 ou quarentena). */
  blocked: boolean
  /** Verdadeiro quando a pausa pertence ao reconciliador P03. */
  p03Owned: boolean
}

export const DEFAULT_STALE_AFTER_MS = 60_000

// ─── Primitivos: só valor literal passa ──────────────────────────────────────

/** Número FINITO ou null. `null`, `undefined`, NaN, Infinity e texto → null. */
export function finiteNumber(raw: unknown): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  return raw
}

/** Inteiro >= 0 ou null (contadores). */
export function countOf(raw: unknown): number | null {
  const n = finiteNumber(raw)
  if (n === null || !Number.isInteger(n) || n < 0) return null
  return n
}

/** Booleano LITERAL ou null. "true", 1 e objetos não viram true. */
export function literalBool(raw: unknown): boolean | null {
  return typeof raw === 'boolean' ? raw : null
}

/** Texto não vazio ou null. */
export function text(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const t = raw.trim()
  return t.length > 0 ? t : null
}

const SECRET_LIKE = /(token|secret|password|api[-_]?key|authorization|bearer)/i

/**
 * Detalhe técnico seguro: uma linha, sem segredo, sem stack e com tamanho
 * limitado. Nunca é usado como motivo humano.
 */
export function sanitizeTechnical(raw: unknown, max = 160): string | null {
  const t = text(raw)
  if (t === null) return null
  const oneLine = t.replace(/\s+/g, ' ').trim()
  if (SECRET_LIKE.test(oneLine)) return 'detalhe omitido (possível credencial)'
  if (/^\s*(Traceback|File ")/.test(t)) return 'detalhe técnico omitido (stack trace)'
  return oneLine.length > max ? `${oneLine.slice(0, max - 1)}…` : oneLine
}

// ─── Vocabulário controlado dos motivos ──────────────────────────────────────

export const UNKNOWN_REASON = 'Motivo não disponível nesta leitura'

const REASON_RULES: { match: RegExp; label: string }[] = [
  { match: /^P03-QUARANTINE/i, label: 'Incidente de execução aberto (reconciliador P03)' },
  { match: /quarantine/i, label: 'Quarentena de execução ativa' },
  { match: /manual[_-]?required/i, label: 'Incidente exige conferência manual' },
  { match: /unknown[_-]?no[_-]?sl|sl[_-]?not[_-]?confirmed|sem sl/i, label: 'Proteção (stop) não confirmada numa execução' },
  { match: /untracked/i, label: 'Posição sem registro correspondente no app' },
  { match: /daily/i, label: 'Limite de perda diária atingido' },
  { match: /weekly|semana/i, label: 'Limite de perda semanal atingido' },
  { match: /consec/i, label: 'Sequência de perdas atingiu o limite' },
  { match: /kill[_-]?switch|manual/i, label: 'Pausa manual (kill switch)' },
]

/** Traduz um motivo do backend. Desconhecido → `UNKNOWN_REASON`. */
export function translateReason(raw: unknown): string {
  const t = text(raw)
  if (t === null) return UNKNOWN_REASON
  for (const rule of REASON_RULES) if (rule.match.test(t)) return rule.label
  return UNKNOWN_REASON
}

// ─── Qualidade de uma leitura ────────────────────────────────────────────────

export interface ReadingQuality {
  /** A leitura ATUAL confirma o valor (sucesso e dentro da tolerância). */
  confirmed: boolean
  /** Existe valor conhecido, mas de leitura antiga ou com erro depois dela. */
  stale: boolean
  /** Nenhum valor conhecido. */
  missing: boolean
  /** Houve erro na leitura mais recente. */
  errored: boolean
  ageMs: number | null
}

export function readingQuality<T>(
  reading: SourceReading<T> | null | undefined,
  nowMs: number,
  staleAfterMs: number,
): ReadingQuality {
  const r = reading ?? emptyReading<T>()
  const hasValue = r.value != null
  const ageMs = r.lastOkAtMs != null ? Math.max(0, nowMs - r.lastOkAtMs) : null
  const tooOld = ageMs == null || ageMs > staleAfterMs
  const errored = r.state === 'ERROR'
  if (!hasValue) {
    return { confirmed: false, stale: false, missing: true, errored, ageMs }
  }
  if (errored || tooOld) {
    return { confirmed: false, stale: true, missing: false, errored, ageMs }
  }
  return { confirmed: true, stale: false, missing: false, errored: false, ageMs }
}

// ─── Interpretação do P03 ────────────────────────────────────────────────────

export interface P03View {
  /** `ok === true` E payload coerente. */
  confirmed: boolean
  openTotal: number | null
  manualRequired: number | null
  quarantineActive: boolean | null
  lastReconciliationAt: string | null
  /** Bloqueio confirmado pelo P03 (incidente aberto ou quarentena). */
  blocked: boolean
  reasons: string[]
  technical: string[]
  /** Por que não foi possível confirmar, quando for o caso. */
  unconfirmedReason: string | null
}

/**
 * Lê o P03 sem transformar ausência em zero. `ok=false`, erro, leitura ausente
 * ou payload incoerente ⇒ NÃO confirmado (nunca "zero incidentes").
 */
export function p03View(
  reading: SourceReading<P03Facts> | null | undefined,
  nowMs: number,
  staleAfterMs: number = DEFAULT_STALE_AFTER_MS,
): P03View {
  const quality = readingQuality(reading, nowMs, staleAfterMs)
  const facts = reading?.value ?? null
  const openTotal = countOf(facts?.open_total)
  const manualRequired = countOf(facts?.manual_required)
  const quarantineActive = literalBool(facts?.quarantine_active)
  const okFlag = literalBool(facts?.ok)
  const technical: string[] = []
  const errTech = sanitizeTechnical(facts?.error ?? reading?.errorReason)
  if (errTech) technical.push(errTech)

  // Bloqueio é fato POSITIVO: vale mesmo sem `ok=true` (incidente conhecido não
  // desaparece porque a leitura degradou).
  const blocked = (openTotal != null && openTotal > 0) || quarantineActive === true
  const reasons: string[] = []
  if (quarantineActive === true) reasons.push('Quarentena de execução ativa')
  if (openTotal != null && openTotal > 0) {
    reasons.push(openTotal === 1
      ? '1 incidente de execução aberto (reconciliador P03)'
      : `${openTotal} incidentes de execução abertos (reconciliador P03)`)
  }
  if (manualRequired != null && manualRequired > 0) {
    reasons.push(manualRequired === 1
      ? '1 incidente exige conferência manual'
      : `${manualRequired} incidentes exigem conferência manual`)
  }

  let unconfirmedReason: string | null = null
  if (quality.missing) unconfirmedReason = 'leitura de incidentes não disponível'
  else if (okFlag !== true) unconfirmedReason = 'serviço de incidentes respondeu sem confirmação'
  else if (openTotal == null || quarantineActive == null) unconfirmedReason = 'resposta de incidentes incompleta'
  else if (quality.stale) unconfirmedReason = 'leitura de incidentes antiga'

  return {
    confirmed: unconfirmedReason === null,
    openTotal, manualRequired, quarantineActive,
    lastReconciliationAt: text(facts?.last_reconciliation_at),
    blocked, reasons, technical, unconfirmedReason,
  }
}

// ─── Estado operacional ──────────────────────────────────────────────────────

const AUTHORITY_MISSING = 'autoridade agregada de entrada (nenhum endpoint do contrato publica este fato)'

/**
 * Deriva o estado operacional a partir dos fatos e da qualidade das leituras.
 *
 * Ordem das decisões: carregando → bloqueio confirmado → sem confirmação →
 * antigo → sem bloqueio confirmado → disponível. Verde exige confirmação
 * explícita; nunca sai de uma ausência.
 */
export function deriveOperationalState(input: OperationalInput): OperationalState {
  const staleAfterMs = input.staleAfterMs ?? DEFAULT_STALE_AFTER_MS
  const nowMs = input.nowMs
  const riskQ = readingQuality(input.risk, nowMs, staleAfterMs)
  const p03 = p03View(input.p03, nowMs, staleAfterMs)
  const riskFacts = input.risk?.value ?? null
  const paused = literalBool(riskFacts?.trading_paused)
  const pauseManual = literalBool(riskFacts?.pause_manual)
  const pauseReasonRaw = text(riskFacts?.pause_reason)

  const missing: string[] = []
  const technical: string[] = []
  const riskTech = sanitizeTechnical(input.risk?.errorReason)
  if (riskTech) technical.push(riskTech)
  technical.push(...p03.technical)

  // ── 1. Nada conhecido ainda em nenhuma fonte ⇒ carregando.
  if (riskQ.missing && p03.openTotal == null && input.risk?.state === 'LOADING') {
    return {
      level: 'LOADING', tone: 'neutral',
      title: 'Lendo estado do bot',
      detail: 'Aguardando a primeira leitura de risco e de incidentes.',
      reasons: [], technical, missing: ['risco', 'incidentes de execução (P03)'],
      lastKnownAtMs: null, stale: false, blocked: false, p03Owned: false,
    }
  }

  // ── 2. Bloqueio CONFIRMADO vence qualquer outra leitura.
  const p03OwnsPause = paused === true && pauseManual !== true
    && pauseReasonRaw != null && /^P03-QUARANTINE/i.test(pauseReasonRaw)
  if (paused === true || p03.blocked) {
    const reasons: string[] = []
    if (paused === true) {
      reasons.push(pauseManual === true
        ? 'Pausa manual (kill switch)'
        : translateReason(pauseReasonRaw))
    }
    reasons.push(...p03.reasons)
    const tech = sanitizeTechnical(pauseReasonRaw)
    if (tech && translateReason(pauseReasonRaw) === UNKNOWN_REASON) technical.push(tech)
    if (paused !== true && p03.blocked) missing.push('pausa de risco (não confirmada nesta leitura)')
    if (p03.unconfirmedReason) missing.push(`detalhes de incidentes: ${p03.unconfirmedReason}`)
    return {
      level: 'BLOCKED', tone: 'danger',
      title: p03OwnsPause || p03.blocked ? 'Bloqueado por incidente de execução' : 'Entradas pausadas',
      detail: p03OwnsPause || p03.blocked
        ? 'O reconciliador P03 mantém o bloqueio até encerrar o incidente. Nem o kill switch nem a virada de dia liberam antes disso.'
        : 'Novas entradas estão pausadas nesta leitura.',
      reasons: reasons.length > 0 ? reasons : [UNKNOWN_REASON],
      technical, missing,
      lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
      stale: riskQ.stale, blocked: true, p03Owned: p03OwnsPause || p03.blocked,
    }
  }

  // ── 3. Sem confirmação atual da leitura de risco.
  if (riskQ.missing || paused == null) {
    missing.push('risco (pausa/drawdown)')
    if (p03.unconfirmedReason) missing.push(`incidentes de execução: ${p03.unconfirmedReason}`)
    return {
      level: 'UNAVAILABLE', tone: 'warn',
      title: 'Sem confirmação nesta leitura',
      detail: riskQ.errored
        ? 'A leitura de risco falhou agora. O estado anterior não autoriza nada.'
        : 'O estado de risco não foi confirmado nesta leitura.',
      reasons: [], technical, missing,
      lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
      stale: false, blocked: false, p03Owned: false,
    }
  }

  // ── 4. Valor conhecido, porém antigo ou seguido de erro.
  if (riskQ.stale) {
    missing.push('confirmação atual do risco')
    if (p03.unconfirmedReason) missing.push(`incidentes de execução: ${p03.unconfirmedReason}`)
    return {
      level: 'STALE', tone: 'warn',
      title: 'Última leitura conhecida',
      detail: 'Mostrando a última leitura bem-sucedida; ela não confirma o estado agora.',
      reasons: [], technical, missing,
      lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
      stale: true, blocked: false, p03Owned: false,
    }
  }

  // ── 5. Autoridade de entrada EXPLÍCITA (hoje inexistente no contrato).
  const authority = input.entryAuthority ?? null
  const authorityQ = readingQuality(authority, nowMs, staleAfterMs)
  const authorityConfirmed = authorityQ.confirmed
    && literalBool(authority?.value?.confirmed) === true
  if (authorityConfirmed && p03.confirmed && !p03.blocked) {
    return {
      level: 'AVAILABLE', tone: 'ok',
      title: 'Disponível para operar',
      detail: 'Autoridade de entrada confirmada nesta leitura. Isso não promete trade nem lucro.',
      reasons: [], technical, missing,
      lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
      stale: false, blocked: false, p03Owned: false,
    }
  }

  // ── 6. Risco confirmado sem pausa. NÃO é verde: falta autoridade agregada.
  missing.push(AUTHORITY_MISSING)
  const riskOnly = (input.scope ?? 'FULL') === 'RISK_ONLY'
  if (riskOnly) {
    // O consumidor declarou que NÃO lê incidentes: isso é lacuna, não "ok".
    missing.push('incidentes de execução (P03): não lidos neste controle')
    return {
      level: 'NO_BLOCK_CONFIRMED', tone: 'info',
      title: 'Risco sem pausa nesta leitura',
      detail: 'Somente o risco foi lido aqui. Incidentes de execução e autoridade de entrada não foram confirmados.',
      reasons: [], technical, missing,
      lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
      stale: false, blocked: false, p03Owned: false,
    }
  }
  if (p03.unconfirmedReason) missing.push(`incidentes de execução: ${p03.unconfirmedReason}`)
  const bothConfirmed = p03.confirmed && !p03.blocked
  return {
    level: bothConfirmed ? 'NO_BLOCK_CONFIRMED' : 'UNAVAILABLE',
    tone: bothConfirmed ? 'info' : 'warn',
    title: bothConfirmed
      ? 'Sem bloqueio de risco/P03 nesta leitura'
      : 'Sem confirmação completa nesta leitura',
    detail: bothConfirmed
      ? 'Risco sem pausa e nenhum incidente de execução aberto. Não é promessa de entrada: o contrato atual não publica autoridade agregada.'
      : 'Risco sem pausa nesta leitura, mas os incidentes de execução não foram confirmados.',
    reasons: [], technical, missing,
    lastKnownAtMs: input.risk?.lastOkAtMs ?? null,
    stale: false, blocked: false, p03Owned: false,
  }
}

// ─── Apresentação auxiliar ───────────────────────────────────────────────────

export const LEVEL_TEXT: Record<OperationalLevel, { short: string; dot: string }> = {
  LOADING: { short: 'Lendo…', dot: 'bg-slate-500' },
  UNAVAILABLE: { short: 'Sem confirmação', dot: 'bg-amber-400' },
  STALE: { short: 'Leitura antiga', dot: 'bg-amber-400' },
  BLOCKED: { short: 'Bloqueado', dot: 'bg-red-500' },
  NO_BLOCK_CONFIRMED: { short: 'Sem bloqueio', dot: 'bg-sky-400' },
  AVAILABLE: { short: 'Disponível', dot: 'bg-emerald-500' },
}

/** "há 12s" / "há 4min" / "—" — a partir de um instante JÁ medido pelo caller. */
export function formatAge(ageMs: number | null): string {
  if (ageMs == null || !Number.isFinite(ageMs) || ageMs < 0) return '—'
  const s = Math.floor(ageMs / 1000)
  if (s < 60) return `há ${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `há ${m}min`
  const h = Math.floor(m / 60)
  if (h < 24) return `há ${h}h`
  return `há ${Math.floor(h / 24)}d`
}
