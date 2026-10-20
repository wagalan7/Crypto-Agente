// ─── Cartões de estado: apresentação PURA (Lote 04) ──────────────────────────
// Recebem fato + qualidade por props. Não buscam, não guardam, não decidem
// nada que já esteja em `lib/operationalState`. Os carregadores continuam nos
// painéis originais (Home/Sistema) — aqui não há fetch nem timer.

import type { ReactNode } from 'react'
import {
  formatAge, LEVEL_TEXT, UNKNOWN_REASON,
  type OperationalState, type P03View, type ReadingQuality,
} from '../../lib/operationalState'

const TONE_BOX: Record<OperationalState['tone'], string> = {
  neutral: 'border-slate-700/70 bg-slate-800/30',
  info: 'border-sky-500/40 bg-sky-500/10',
  ok: 'border-emerald-500/40 bg-emerald-500/10',
  warn: 'border-amber-500/40 bg-amber-500/10',
  danger: 'border-red-500/50 bg-red-500/10',
}
const TONE_TEXT: Record<OperationalState['tone'], string> = {
  neutral: 'text-slate-200',
  info: 'text-sky-100',
  ok: 'text-emerald-100',
  warn: 'text-amber-100',
  danger: 'text-red-100',
}
/** Ícone textual acessível: a cor nunca é o único sinal. */
const TONE_ICON: Record<OperationalState['tone'], string> = {
  neutral: '○', info: 'i', ok: '✓', warn: '!', danger: '■',
}

/** Lista de causas/lacunas que NÃO corta texto (quebra linha e mantém tudo). */
function ReasonList({ title, items, cls }: { title: string; items: string[]; cls: string }) {
  if (items.length === 0) return null
  return (
    <div className="mt-2">
      <div className={`card-label ${cls}`}>{title}</div>
      <ul className="mt-1 space-y-1">
        {items.map((r, i) => (
          <li key={`${i}-${r.slice(0, 24)}`} className="text-[12px] leading-snug break-words text-slate-200">
            <span aria-hidden="true" className="text-slate-500">— </span>{r}
          </li>
        ))}
      </ul>
    </div>
  )
}

export interface OperationalStateCardProps {
  state: OperationalState
  /** Idade da leitura em ms, já medida pelo chamador (não renova ao renderizar). */
  ageMs: number | null
  /** Abre o diagnóstico EXISTENTE (Sistema). Nunca dispara retomada/POST. */
  onOpenDiagnosis?: () => void
  diagnosisLabel?: string
  compact?: boolean
  children?: ReactNode
}

/**
 * Cartão único de estado operacional. Mostra nível, motivo humano, o que não
 * foi confirmado e o instante da última leitura boa. Nunca oferece botão que
 * contorne o reconciliador.
 */
export function OperationalStateCard({
  state, ageMs, onOpenDiagnosis, diagnosisLabel, compact, children,
}: OperationalStateCardProps) {
  const level = LEVEL_TEXT[state.level]
  return (
    <section
      aria-label="Estado operacional do bot"
      data-testid="operational-state"
      data-level={state.level}
      className={`rounded-2xl border px-4 py-3 ${TONE_BOX[state.tone]}`}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full border border-current text-[11px] font-bold ${TONE_TEXT[state.tone]}`}
        >
          {TONE_ICON[state.tone]}
        </span>
        <div className="min-w-0 flex-1">
          <div className={`text-[14px] font-bold ${TONE_TEXT[state.tone]}`}>{state.title}</div>
          <p className="mt-0.5 text-[12.5px] leading-snug text-slate-300 break-words">{state.detail}</p>

          {!compact && <ReasonList title="Motivo" items={state.reasons} cls="text-slate-400" />}
          {!compact && <ReasonList title="Não confirmado nesta leitura" items={state.missing} cls="text-slate-500" />}

          {!compact && state.technical.length > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer text-[11.5px] text-slate-400">Detalhe técnico</summary>
              <ul className="mt-1 space-y-1">
                {state.technical.map((t, i) => (
                  <li key={i} className="font-mono text-[11px] leading-snug text-slate-400 break-words">{t}</li>
                ))}
              </ul>
            </details>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
            <span className="num">
              {state.lastKnownAtMs != null
                ? `Última leitura confirmada ${formatAge(ageMs)}`
                : 'Nenhuma leitura confirmada ainda'}
            </span>
            {state.stale && <span className="text-amber-300">dado de leitura anterior</span>}
            <span className="sr-only">{`Nível: ${level.short}`}</span>
          </div>

          {children}

          {onOpenDiagnosis && (
            <button
              type="button"
              onClick={onOpenDiagnosis}
              className="mt-3 rounded-lg border border-slate-600 bg-slate-800/60 px-3 py-1.5 text-[12px] font-semibold text-slate-200 hover:bg-slate-700/60"
            >
              {diagnosisLabel ?? 'Ver diagnóstico'} ›
            </button>
          )}
        </div>
      </div>
    </section>
  )
}

// ─── Incidentes P03 ──────────────────────────────────────────────────────────

export interface P03CardProps {
  view: P03View
  ageMs: number | null
  onOpenDiagnosis?: () => void
}

/**
 * Incidentes de execução e quarentena. Contagem ausente aparece como
 * "não confirmado", nunca como zero. Sem botão de retomada/retry.
 */
export function P03IncidentCard({ view, ageMs, onOpenDiagnosis }: P03CardProps) {
  const tone: OperationalState['tone'] = view.blocked ? 'danger' : view.confirmed ? 'info' : 'warn'
  return (
    <section
      aria-label="Incidentes de execução (P03)"
      data-testid="p03-card"
      data-blocked={view.blocked ? 'true' : 'false'}
      data-confirmed={view.confirmed ? 'true' : 'false'}
      className={`rounded-2xl border px-4 py-3 ${TONE_BOX[tone]}`}
    >
      <div className="card-label text-slate-400">Execução · reconciliador P03</div>
      <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-2">
        <Metric label="Incidentes abertos" value={view.openTotal} />
        <Metric label="Exigem conferência" value={view.manualRequired} />
        <div className="min-w-[120px]">
          <div className="text-[11px] text-slate-500">Quarentena</div>
          <div className="text-[13px] font-bold text-slate-100">
            {view.quarantineActive == null
              ? 'não confirmada'
              : view.quarantineActive ? 'ativa' : 'desligada'}
          </div>
        </div>
      </div>

      {view.reasons.length > 0 && (
        <ReasonList title="Causa" items={view.reasons} cls="text-slate-400" />
      )}
      {!view.confirmed && (
        <p className="mt-2 text-[12px] leading-snug text-amber-200">
          Detalhes indisponíveis: {view.unconfirmedReason ?? 'leitura sem confirmação'}. Ausência de
          leitura não é ausência de incidente.
        </p>
      )}
      {view.technical.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11.5px] text-slate-400">Detalhe técnico</summary>
          <ul className="mt-1 space-y-1">
            {view.technical.map((t, i) => (
              <li key={i} className="font-mono text-[11px] text-slate-400 break-words">{t}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="mt-2 text-[11px] text-slate-500 num">
        {view.lastReconciliationAt
          ? `Última reconciliação do backend: ${view.lastReconciliationAt}`
          : 'Instante da última reconciliação não informado'}
        {ageMs != null && ` · leitura ${formatAge(ageMs)}`}
      </div>
      {onOpenDiagnosis && (
        <button
          type="button"
          onClick={onOpenDiagnosis}
          className="mt-3 rounded-lg border border-slate-600 bg-slate-800/60 px-3 py-1.5 text-[12px] font-semibold text-slate-200 hover:bg-slate-700/60"
        >
          Ver diagnóstico de execução ›
        </button>
      )}
    </section>
  )
}

function Metric({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="min-w-[120px]">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`num text-[15px] font-bold ${value == null ? 'text-slate-400' : value > 0 ? 'text-red-300' : 'text-slate-100'}`}>
        {value == null ? 'não confirmado' : value}
      </div>
    </div>
  )
}

// ─── Resultado por FONTE ─────────────────────────────────────────────────────

export type ResultSource = 'SETUPS' | 'REAL_BOT' | 'MANAGED' | 'MANUAL_EXTERNO' | 'BACKTEST'

export const SOURCE_LABEL: Record<ResultSource, string> = {
  SETUPS: 'Resultado dos setups',
  REAL_BOT: 'Resultado real do bot',
  MANAGED: 'Resultado gerenciado',
  MANUAL_EXTERNO: 'Posições externas (manuais)',
  BACKTEST: 'Resultado modelado (backtest)',
}

export interface SourceResultCardProps {
  source: ResultSource
  /** Janela REAL da resposta ("hoje (UTC)", "30 dias", …). */
  window: string
  /** Valor principal já formatado pelo chamador, ou null quando desconhecido. */
  primary: string | null
  /** Unidade/qualificação do valor principal ("% da banca", "R", …). */
  unit?: string | null
  secondary?: string | null
  quality: ReadingQuality
  /** Frase explicando o que a fonte prova — e o que NÃO prova. */
  note: string
  /** Mensagem quando não há fonte legítima (ex.: inventário externo). */
  unavailableText?: string
  positive?: boolean | null
  children?: ReactNode
}

/**
 * Cartão de resultado de UMA fonte, com janela e unidade explícitas. Fonte sem
 * leitura mostra texto de indisponibilidade — nunca zero nem total inventado.
 */
export function SourceResultCard({
  source, window: windowLabel, primary, unit, secondary, quality, note,
  unavailableText, positive, children,
}: SourceResultCardProps) {
  const unknown = primary == null || quality.missing
  const valueCls = unknown
    ? 'text-slate-400'
    : positive === true ? 'text-emerald-300'
    : positive === false ? 'text-red-300'
    : 'text-slate-100'
  return (
    <section
      aria-label={`${SOURCE_LABEL[source]} · ${windowLabel}`}
      data-testid={`source-${source}`}
      data-quality={quality.missing ? 'MISSING' : quality.stale ? 'STALE' : 'CONFIRMED'}
      className="rounded-2xl border border-slate-800 bg-[#0f1524] p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="card-label text-slate-400">{SOURCE_LABEL[source]}</h3>
        <span className="rounded border border-slate-700 px-1.5 py-px text-[10.5px] text-slate-400">
          {windowLabel}
        </span>
      </div>

      {unknown ? (
        <p className="mt-2 text-[13px] leading-snug text-slate-300">
          {unavailableText ?? 'Valor não disponível nesta leitura.'}
        </p>
      ) : (
        <>
          <div className={`num mt-2 text-[28px] font-extrabold leading-none ${valueCls}`}>
            {primary}
            {unit && <span className="ml-1 align-baseline text-[13px] font-semibold text-slate-400">{unit}</span>}
          </div>
          {secondary && <div className="num mt-1.5 text-[12.5px] text-slate-400">{secondary}</div>}
        </>
      )}

      {quality.stale && !quality.missing && (
        <p className="mt-2 text-[11.5px] text-amber-300">
          Última leitura conhecida ({formatAge(quality.ageMs)}); não confirma o valor agora.
        </p>
      )}
      <p className="mt-2 text-[11.5px] leading-snug text-slate-500 break-words">{note}</p>
      {children}
    </section>
  )
}

// ─── Inventário de posições ──────────────────────────────────────────────────

/** Estado vazio da lista da Home: conhecido anteriormente não é zero atual. */
export function PositionsEmptyState({ quality }: { quality: ReadingQuality }) {
  return (
    <div data-testid="positions-empty" data-quality={quality.missing ? 'MISSING' : quality.confirmed ? 'CONFIRMED' : 'STALE'}
      className={`py-8 text-center text-sm ${quality.confirmed ? 'text-slate-500' : 'text-amber-200'}`}>
      {quality.missing
        ? 'Não foi possível ler as posições nesta leitura.'
        : quality.confirmed
          ? 'Nenhum registro aberto no app (confirmado nesta leitura).'
          : 'A última leitura conhecida não tinha registros abertos no app; a situação atual não foi confirmada.'}
    </div>
  )
}

export interface PositionsInventoryCardProps {
  quality: ReadingQuality
  /** Quantidade CONFIRMADA de posições do bot; null = não confirmada. */
  botCount: number | null
  /** Quantidade CONFIRMADA de registros manuais no app; null = não confirmada. */
  managedManualCount: number | null
  onOpenTrades?: () => void
  children?: ReactNode
}

/**
 * Inventário por ORIGEM. `/api/real-trades` é o registro do app, não a conta:
 * zero BOT confirmado é zero BOT — não "zero posições na Binance".
 */
export function PositionsInventoryCard({
  quality, botCount, managedManualCount, onOpenTrades, children,
}: PositionsInventoryCardProps) {
  return (
    <section
      aria-label="Posições por origem"
      data-testid="positions-inventory"
      data-quality={quality.missing ? 'MISSING' : quality.stale ? 'STALE' : 'CONFIRMED'}
      className="rounded-2xl border border-slate-800 bg-[#0f1524] p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="card-label text-slate-400">Posições por origem</h3>
        {onOpenTrades && (
          <button
            type="button"
            onClick={onOpenTrades}
            className="text-[11.5px] font-semibold text-cyan-300 hover:text-cyan-200"
          >
            gerenciar ›
          </button>
        )}
      </div>

      {quality.missing ? (
        <p className="mt-2 text-[13px] leading-snug text-amber-200">
          Lista de posições não disponível nesta leitura. Isso não é lista vazia.
        </p>
      ) : (
        <div className="mt-2 flex flex-wrap gap-x-6 gap-y-3">
          <Metric label="Registradas pelo bot" value={botCount} />
          <Metric label="Registradas como manuais no app" value={managedManualCount} />
        </div>
      )}

      <p className="mt-3 text-[11.5px] leading-snug text-slate-500">
        Posições externas da conta não estão disponíveis nesta leitura: o app mostra os
        registros dele (<span className="font-mono">/api/real-trades</span>), não o inventário
        da Binance. Zero registros do bot não significa zero posições na conta.
      </p>
      {quality.stale && !quality.missing && (
        <p className="mt-2 text-[11.5px] text-amber-300">
          Última leitura conhecida ({formatAge(quality.ageMs)}).
        </p>
      )}
      {children}
    </section>
  )
}

export { UNKNOWN_REASON }
