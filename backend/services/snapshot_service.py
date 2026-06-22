"""
Snapshot Service — salva recomendações ao serem geradas, monitora outcome
(stop/tp atingido) e agrega P&L diário.

Como funciona:
- Quando o frontend recebe um lote de recomendações via /recommendations-batch,
  o backend persiste cada uma com `status="open"`.
- Periodicamente (a cada 5 min), um job checa o preço atual dos snapshots
  abertos e marca won_tp1/won_tp2/lost conforme a barreira tocada.
- "Expired": se passar 48h sem hit, marca expired (não conta no P&L).
- Snapshot é desduplicado por (symbol, timeframe, direction, entry) dentro de
  uma janela de 2h pra não inflar com a varredura rodando a cada 2 min.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, func, update, delete

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot
from services.push_service import notify_outcome

log = logging.getLogger(__name__)

# ── Configuração ─────────────────────────────────────────────────────────
DEDUP_WINDOW_HOURS = 2       # mesma rec não entra 2× nesse intervalo

# Decouple (EXEC_UNIVERSE_DECOUPLE): status reservado pros snapshots do universo
# AMPLO (só display/push). É invisível a TODO agregador de aprendizado/risco/PnL
# porque eles filtram status resolvido, =="open", ou realized_r IS NOT NULL — e
# 'wide' não é nenhum deles (realized_r fica NULL, nunca resolve). check_open
# também o ignora (status != "open") → zero carga de candles. Só existe pra
# dedup DB-backed do push (sem storm no restart). Podado por TTL.
WIDE_DISPLAY_STATUS = "wide"
WIDE_DISPLAY_TTL_HOURS = float(os.getenv("WIDE_DISPLAY_TTL_HOURS", "6"))

# ── Opção B: rastreio do universo AMPLO p/ HISTÓRICO (observação) ─────────
# Quando WIDE_TRACKING_ENABLED, os snapshots 'wide' deixam de ser podados aos
# 6h e passam a ser RASTREADOS pelo mesmo motor de outcome dos trades reais
# (check_wide_snapshots ≈ check_open_snapshots), seguindo o ciclo de vida
# completo (abertos → resolvidos → vencedores/perdedores). MAS de forma
# ISOLADA das estatísticas do bot: a coluna `realized_r` fica SEMPRE NULL e o
# status resolvido vai pra um namespace próprio ("wide_won_tp2", "wide_lost"…).
# O R/PnL de observação é gravado em features['wide_outcome'] (JSON), NUNCA na
# coluna compartilhada — logo todo agregador de aprendizado/risco/PnL (que
# filtra realized_r IS NOT NULL ou status resolvido conhecido) continua cego a
# eles, POR CONSTRUÇÃO. Default OFF: nada muda no bot ao subir.
WIDE_TRACKING_ENABLED = os.getenv("WIDE_TRACKING_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# Teto de quantos 'wide' abertos são checados por ciclo (limita carga de candles
# no proxy — cada um faz fetch_ohlcv). Rotaciona pelos menos-recentemente-checados.
WIDE_TRACKING_MAX = int(os.getenv("WIDE_TRACKING_MAX", "40"))
# Prefixo do namespace de status resolvido da observação.
WIDE_STATUS_PREFIX = "wide_"
# Status resolvidos de observação (espelham os reais, com prefixo).
WIDE_RESOLVED_STATUSES = (
    "wide_won_tp1", "wide_won_tp1_be", "wide_won_tp2", "wide_lost", "wide_expired",
)
# Teto absoluto: qualquer trade mais velho que isto é encerrado. Antes era 48h
# hardcoded, o que cortava SWING cedo demais (swing aguenta dias) e desalinhava
# do gerenciador dos trades reais (TIME_STOP_SWING_MIN = 7 dias). Agora é env,
# default 168h (7 dias) — alinhado ao swing real. Não reescreve histórico:
# só vale pros snapshots resolvidos daqui pra frente.
EXPIRY_HOURS = float(os.getenv("SNAPSHOT_EXPIRY_HOURS", "168"))

# 1 rec por SÍMBOLO (não só por símbolo+direção): bloqueia gerar uma rec
# na direção OPOSTA enquanto já há uma aberta no mesmo símbolo. Evita o
# absurdo de ter long e short simultâneos na mesma moeda (sinais contraditórios
# poluindo o painel). Default ligado; desligue com ONE_REC_PER_SYMBOL=false.
ONE_REC_PER_SYMBOL = os.getenv("ONE_REC_PER_SYMBOL", "true").strip().lower() in ("1", "true", "yes")

# Time-stop por timeframe: se TP1 não bater em N candles, encerra o trade
# (evita "morte lenta" que trava capital sem stop nem TP). Cada TF tem
# horizonte próprio — scalp deve resolver em horas, swing aguenta dias.
# Valor = horas máximas SEM tocar TP1. Se TP1 já tocou, NÃO aplica
# (trade está em lucro, deixa o trail/TP2 cuidar).
#
# SWING (4h+) usa o mesmo horizonte do gerenciador dos trades reais
# (TIME_STOP_SWING_MIN = 10080 min = 168h = 7 dias). Antes 4h→36h e
# 6h/8h/12h/1d→48h (cappado), o que expirava swing cedo demais e treinava
# o learner com uma política de saída diferente da do bot real. Via env
# pra ajustar sem mexer em código.
_SWING_HOURS = float(os.getenv("SNAPSHOT_TIME_STOP_SWING_HOURS", "168"))
# HODL (3d) precisa de horizonte próprio: o candle de 3d tem 72h, então 168h
# dava só ~2,3 candles de pista antes de expirar — cortava o setup hodl (atr_mult
# 8.0, feito pra cavalgar movimento longo) antes de ele andar. Alinhado ao 1d,
# que ganha ~7 candles (168h/24h): 3d com ~7 candles = 504h (21 dias).
_HODL_HOURS = float(os.getenv("SNAPSHOT_TIME_STOP_HODL_HOURS", "504"))
TIME_STOP_HOURS_BY_TF = {
    "1m": 1, "3m": 2, "5m": 3, "15m": 4, "30m": 8,
    "1h": 12, "2h": 24,
    "4h": _SWING_HOURS, "6h": _SWING_HOURS,
    "8h": _SWING_HOURS, "12h": _SWING_HOURS, "1d": _SWING_HOURS,
    "3d": _HODL_HOURS,
}


def _time_stop_hours(tf: str) -> float:
    """Horas máximas sem tocar TP1 antes de encerrar por time-stop."""
    return float(TIME_STOP_HOURS_BY_TF.get(tf, EXPIRY_HOURS))


def _expiry_ceiling_hours(tf: str) -> float:
    """Teto absoluto por TF: nunca menor que o time-stop do próprio TF.

    EXPIRY_HOURS (168h) é o piso pra TFs curtos/médios, mas TFs longos (3d=504h)
    têm time-stop maior que o teto — sem isto o teto de 168h sobreporia o
    time-stop e fecharia o hodl cedo demais. max() mantém 168h pra todo mundo e
    estende só onde o TF exige mais pista.
    """
    return max(EXPIRY_HOURS, _time_stop_hours(tf))
# ── R esperado (Step 2b v2: parcial 50% no TP1 + BE+ assimétrico) ────────
# Premissa: ao tocar TP1, fecha 50% da posição (+1R em metade), restante segue
# com stop EFETIVO em "BE+" (entry + 0.2R lock) e trail por ATR mais largo.
# Mudança vs v1: trail era 1.5×ATR e piso em entry puro. Resultado: 55% dos
# TP1-hits viravam won_tp1_be (+0.5R) em vez de won_tp2 (+1.5R). Diagnóstico
# via /api/debug/status-distribution em 2026-05 mostrou 23 won_tp1_be vs 19
# won_tp2 nos últimos 30 dias. Conclusão: trail apertado demais. Agora:
#   • Trail mais largo (K=2.2) → dá respiro pra retração normal
#   • Buffer mínimo (peak precisa avançar ≥0.5×ATR além do TP1 pra trail ativar)
#   • Piso = BE+ lock (0.2R) → ainda garante lucro mínimo
# R reportado é média ponderada das duas metades:
#   • Parcial sai em TP1 (+1R) → metade = +0.5R
#   • Final em TP2 (+2R)       → metade = +1.0R  → total +1.5R (won_tp2)
#   • Final em BE+ lock        → metade = +0.2R  → total +0.6R (won_tp1_be)
#   • Final expira após TP1    → conservador     → total +0.6R (won_tp1)
# Stop original (antes de TP1) = -1R cheio (não houve parcial).
REALIZED_R_TP1 = 0.6           # 50% TP1 (+1R) + 50% BE+lock (+0.2R) = +0.6R
REALIZED_R_TP2 = 1.5           # 50% TP1 + 50% TP2
REALIZED_R_STOP = -1.0         # stop original (antes de TP1)
REALIZED_R_BREAKEVEN = 0.6     # 50% TP1 (+1R) + 50% BE+lock (+0.2R) = +0.6R

# Trail por ATR após TP1 hit (v2 — mais largo).
ATR_TRAIL_K = 2.2

# BE+ lock: parcela do range (tp1-entry) que vira piso do stop após TP1.
# 0.2 = 20% do caminho do entry até o TP1, garantindo +0.2R no pior caso.
BE_PLUS_LOCK_R = 0.2

# Buffer antes do trail por ATR ativar: peak precisa avançar ≥ esse múltiplo
# do ATR ALÉM do TP1 antes do trail dinâmico começar a apertar. Evita o
# whipsaw imediato pós-TP1 (vela seguinte retrai e mata posição).
TRAIL_ACTIVATION_BUFFER_ATR = 0.5


def _extract_features(
    rec: Dict[str, Any], created_at: datetime, regime_label: str | None = None
) -> Dict[str, Any]:
    """Captura vetor de features pro learning loop. Robust a campos ausentes.

    `regime_label` (opcional): rótulo do regime de mercado VIGENTE na criação do
    snapshot (NORMAL / RISK_OFF / ALT_DANGER / BTC_DOMINANT / ALT_RISK_OFF). É o
    único jeito de auditar desempenho por regime DEPOIS — o regime não é
    reconstruível com fidelidade pós-fato. Só novos snapshots terão; antigos
    saem como None (a auditoria de regime acumula a partir do deploy)."""
    sig = rec.get("signal") or {}
    if not isinstance(sig, dict):
        return {
            "hour_utc": created_at.hour,
            "day_of_week": created_at.weekday(),
            "regime": regime_label,
        }

    ind = sig.get("indicators") or {}
    mtf = sig.get("mtf") or {}
    confluence = sig.get("confluence") or {}
    derivatives = sig.get("derivatives") or {}
    patterns = sig.get("patterns") or []

    # Padrões: lista de strings
    pattern_types = []
    if isinstance(patterns, list):
        for p in patterns:
            if isinstance(p, dict):
                t = p.get("type")
                if t:
                    pattern_types.append(t)

    # ATR como % do entry (medida de volatilidade)
    atr = ind.get("atr")
    entry = sig.get("entry") or rec.get("entry") or 0
    atr_pct = None
    if atr and entry:
        try:
            atr_pct = round((float(atr) / float(entry)) * 100, 3)
        except Exception:
            atr_pct = None

    return {
        "rsi": ind.get("rsi"),
        "adx": ind.get("adx"),
        "atr_pct": atr_pct,
        "mtf_score": mtf.get("alignment_score") if mtf else None,
        "mtf_aligned": mtf.get("aligned_count") if mtf else None,
        "confluence_pct": confluence.get("pct") if confluence else None,
        "patterns": pattern_types,
        "funding_pct": derivatives.get("funding_rate_pct") if derivatives else None,
        "funding_sentiment": derivatives.get("funding_sentiment") if derivatives else None,
        "oi_change_pct": derivatives.get("oi_change_24h_pct") if derivatives else None,
        "hour_utc": created_at.hour,
        "day_of_week": created_at.weekday(),    # 0 = Monday
        # Veredito de QUALIDADE do bot no momento da criação (R:R / P(TP1) /
        # liquidez — mesma lógica de exec_verdict). Persistido pra o painel poder
        # filtrar "só aprovados pelo bot" mesmo em trades de dias anteriores, com
        # o veredito EXATO da decisão (não um recompute aproximado). None se a rec
        # não trouxe bot_verdict.
        "bot_verdict_ok": (rec.get("bot_verdict") or {}).get("ok"),
        # Tipo da zona de entrada (Order Block / FVG / Value Area…) — pro card de
        # Abertos nomear o padrão igual ao painel de Recomendações. Só novos
        # snapshots terão; antigos saem como None (fail-soft no front).
        "entry_zone_type": (
            rec.get("entry_zone_type")
            or ((sig.get("trade_plan") or {}).get("entry_zone") or {}).get("type")
        ),
        # Regime de mercado vigente na criação (pro audit por regime — #A).
        "regime": regime_label,
        # Anti-chase: distância (×ATR) que o preço já andou a favor (proximity) e
        # tamanho da perna desde a base (struct_chase). Persistidos PRÉ-gate → o
        # snapshot registra o desfecho MESMO dos setups que o proximity/struct_chase
        # depois rejeitam na execução. Sem isso é impossível medir contrafactual
        # "se eu tivesse afrouxado o gate, teria ganho ou perdido?". Lidos da MESMA
        # chave que os gates usam (rec['chase_atr'] / rec['struct_chase_atr']).
        "chase_atr": rec.get("chase_atr"),
        "struct_chase_atr": rec.get("struct_chase_atr"),
        "retest_armed": rec.get("retest_armed"),
    }


async def _current_regime_label() -> str | None:
    """Rótulo do regime de mercado AGORA (fail-soft, cache de 10min no service).
    Chamado 1x por batch de save — não por rec. None se indisponível."""
    try:
        from services.regime_service import get_regime_status
        rg = await get_regime_status()
        return (rg or {}).get("regime")
    except Exception as e:
        log.debug(f"[snapshot] regime label indisponível: {e}")
        return None


def _tf_rank(tf: str) -> int:
    """Rank do timeframe pra comparar 'tamanho' (SCALP < DAY < SWING).
    Maior número = TF maior. Usado pra bloquear duplicação intra-direção.
    Sincronizar com frontend/DailyPnLPanel.operationType()."""
    if not tf:
        return 0
    t = tf.strip().lower()
    if t in ("1m", "3m", "5m", "15m"):
        return 1  # SCALP
    if t in ("30m", "1h", "2h"):
        return 2  # DAY
    return 3      # SWING (4h, 1d, 1w, etc)


async def _has_open_opposite_real_trade(session, symbol: str, direction: str) -> bool:
    """True se existe RealTrade auto OPEN no símbolo em direção OPOSTA.

    Usado por save_recommendations rule (b): quando há um trade real oposto
    aberto, a rec oposta NÃO deve ser bloqueada — ela precisa fluir até o
    avaliador de flip (Fase 2) no shadow_trade_service. Bloquear aqui mataria
    o auto-flip. Quando NÃO há trade real oposto, o ONE_REC_PER_SYMBOL segue
    valendo (evita long+short simultâneo só de snapshots informativos)."""
    try:
        from models.real_trade import RealTrade
        opposite_side = "long" if direction == "short" else "short"
        stmt = select(RealTrade.id).where(
            and_(
                RealTrade.symbol == symbol,
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == opposite_side,
            )
        ).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        log.warning(f"[snapshot] check trade real oposto falhou {symbol}: {e}")
        return False


async def save_recommendations(recommendations: List[Dict[str, Any]]) -> int:
    """
    Salva snapshots de recomendações novas (desduplicadas).
    Retorna quantos foram efetivamente inseridos.

    Side-effect: marca cada rec com `_just_saved=True` se foi inserida nesta
    chamada (não duplicata). Permite ao caller filtrar quais notificar via
    push sem precisar re-consultar o DB.

    Regras de bloqueio (Fase 1 — 1 rec ativa por símbolo+direção):
      1. Dedup janela curta: mesmo (symbol, tf, direction) recente → skip
      2. Anti-duplicação intra-direção: se já existe snapshot OPEN com
         (symbol, direction) MESMO TF ou TF MENOR → skip. Só permite
         se a nova rec é TF MAIOR (upgrade — Fase 3 trata a transição).
    """
    if not DB_ENABLED or not recommendations:
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_WINDOW_HOURS)
    _regime_label = await _current_regime_label()  # 1x por batch (#A — audit por regime)

    async with get_session() as session:
        for rec in recommendations:
            try:
                # Dedup: existe registro recente do mesmo setup?
                # status != "wide": um snapshot do universo AMPLO (display/push,
                # decouple) NÃO deve bloquear o save de EXECUÇÃO do mesmo setup —
                # senão um setup que migrou de "amplo" pra "executado" perderia
                # o registro real e não alimentaria o auto-learner.
                stmt = select(RecommendationSnapshot.id).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.timeframe == rec["timeframe"],
                        RecommendationSnapshot.direction == rec["direction"],
                        RecommendationSnapshot.status != WIDE_DISPLAY_STATUS,
                        RecommendationSnapshot.created_at >= cutoff,
                    )
                ).limit(1)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing:
                    rec["_just_saved"] = False
                    continue

                # Fase 1: bloqueia rec adicional pro mesmo (symbol, direction)
                # se já houver snapshot OPEN em TF igual ou MENOR. Permite
                # passar quando a nova é TF MAIOR — vira candidato a upgrade.
                # Busca TODOS os abertos do símbolo (qualquer direção) pra também
                # aplicar o "1 rec por símbolo" (bloqueia direção oposta).
                new_rank = _tf_rank(rec.get("timeframe"))
                open_stmt = select(
                    RecommendationSnapshot.id,
                    RecommendationSnapshot.timeframe,
                    RecommendationSnapshot.direction,
                ).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.status == "open",
                    )
                )
                open_rows = (await session.execute(open_stmt)).all()
                same_dir_rows = [r for r in open_rows if r.direction == rec["direction"]]
                opp_dir_rows = [r for r in open_rows if r.direction != rec["direction"]]

                # (a) Mesma direção: se qualquer aberto tem rank >= novo →
                # bloqueia (mesmo TF ou maior já cobre). Só passa se TODOS os
                # abertos forem TF MENOR que o novo (upgrade legítimo).
                if any(_tf_rank(r.timeframe) >= new_rank for r in same_dir_rows):
                    rec["_just_saved"] = False
                    log.debug(
                        f"[snapshot] skip rec {rec['symbol']} {rec['direction']} "
                        f"tf={rec.get('timeframe')} — já há OPEN em TF >= nesse par"
                    )
                    continue

                # (b) 1 rec por símbolo: bloqueia direção OPOSTA enquanto há
                # uma aberta no mesmo símbolo (evita long+short simultâneo).
                # EXCEÇÃO: se há um TRADE REAL aberto na direção oposta, a rec
                # oposta DEVE fluir (não bloquear) pra alimentar o avaliador de
                # flip (Fase 2) no shadow_trade_service. Bloquear aqui setaria
                # _just_saved=False e o flip nunca seria avaliado.
                if ONE_REC_PER_SYMBOL and opp_dir_rows:
                    if await _has_open_opposite_real_trade(
                        session, rec["symbol"], rec["direction"]
                    ):
                        log.debug(
                            f"[snapshot] rec OPOSTA {rec['symbol']} {rec['direction']} "
                            f"tf={rec.get('timeframe')} FLUI — há trade real oposto aberto "
                            f"(candidato a flip)"
                        )
                        # não dá continue — segue pro insert abaixo (flip pipeline)
                    else:
                        rec["_just_saved"] = False
                        log.debug(
                            f"[snapshot] skip rec {rec['symbol']} {rec['direction']} "
                            f"tf={rec.get('timeframe')} — já há OPEN na direção oposta "
                            f"(ONE_REC_PER_SYMBOL, sem trade real)"
                        )
                        continue

                # tp1 pode estar em signal.tp1 ou ausente
                tp1 = None
                sig = rec.get("signal") or {}
                if isinstance(sig, dict):
                    tp1 = sig.get("tp1")

                snap = RecommendationSnapshot(
                    symbol=rec["symbol"],
                    timeframe=rec["timeframe"],
                    tier=rec["tier"],
                    direction=rec["direction"],
                    entry=float(rec["entry"]),
                    stop_loss=float(rec["stop_loss"]),
                    tp1=float(tp1) if tp1 is not None else None,
                    tp2=float(rec["tp2"]),
                    score=float(rec["score"]),
                    risk_reward=float(rec["risk_reward"]),
                    leverage=int(rec.get("leverage", 1)),
                    risk_pct=float(rec.get("risk_pct", 1.0)),
                    stop_distance_pct=float(rec.get("stop_distance_pct", 0.0)),
                    status="open",
                    created_at=now,
                    features=_extract_features(rec, now, _regime_label),
                )
                session.add(snap)
                rec["_just_saved"] = True
                inserted += 1
            except Exception as e:
                log.warning(f"Falha ao salvar snapshot {rec.get('symbol')}: {e}")
        await session.commit()

    if inserted:
        log.info(f"Snapshots persistidos: {inserted}")
    return inserted


async def save_wide_display_snapshots(recommendations: List[Dict[str, Any]]) -> int:
    """
    Decouple (EXEC_UNIVERSE_DECOUPLE): persiste os snapshots do universo AMPLO
    (display/push) com status='wide'. Esse status NÃO é considerado por NENHUM
    agregador de aprendizado/risco/PnL — todos filtram status resolvido, =="open",
    ou realized_r IS NOT NULL, e 'wide' não é nenhum deles (realized_r=NULL, nunca
    resolve). Logo o auto-learner / circuit breaker / planejador da banca do PRD
    ficam INTOCADOS por construção (não por filtro espalhado que poderia falhar).

    Propósito único:
      1. dedup DB-backed → não repete push do mesmo setup na janela (sobrevive a
         restart ⇒ sem storm de push no deploy);
      2. marca rec['_just_saved'] nos recém-inseridos pro batch de push.

    NÃO são rastreados por check_open_snapshots (status != "open") ⇒ zero carga
    extra de candles. Linhas wide mais velhas que WIDE_DISPLAY_TTL_HOURS são
    podadas aqui (não há outcome a preservar). Isolado de save_recommendations
    de propósito: o caminho de EXECUÇÃO/learner não muda em nada.
    """
    if not DB_ENABLED or not recommendations:
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_WINDOW_HOURS)
    _regime_label = await _current_regime_label()  # 1x por batch (#A — audit por regime)
    # Sem rastreio (Opção B OFF): poda 'wide' aos WIDE_DISPLAY_TTL_HOURS — só a
    # dedup importa, não há outcome a guardar. Com rastreio ON: NÃO podar aos 6h,
    # senão mataríamos snapshots ainda EM VOO antes do outcome. Deixa viver até o
    # teto de expiração (EXPIRY_HOURS); o check_wide_snapshots resolve antes e os
    # resolvidos (status 'wide_*') são podados separadamente lá, por outcome_at.
    _wide_prune_h = EXPIRY_HOURS if WIDE_TRACKING_ENABLED else WIDE_DISPLAY_TTL_HOURS
    prune_before = now - timedelta(hours=_wide_prune_h)

    async with get_session() as session:
        # Poda linhas wide antigas (só a dedup importa; sem outcome a guardar).
        try:
            await session.execute(
                delete(RecommendationSnapshot).where(
                    and_(
                        RecommendationSnapshot.status == WIDE_DISPLAY_STATUS,
                        RecommendationSnapshot.created_at < prune_before,
                    )
                )
            )
        except Exception as e:
            log.warning(f"[wide] prune falhou: {e}")

        for rec in recommendations:
            try:
                # Dedup só contra OUTROS snapshots 'wide' (namespace próprio —
                # não interage com a dedup/anti-dup do caminho de execução).
                stmt = select(RecommendationSnapshot.id).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.timeframe == rec["timeframe"],
                        RecommendationSnapshot.direction == rec["direction"],
                        RecommendationSnapshot.status == WIDE_DISPLAY_STATUS,
                        RecommendationSnapshot.created_at >= cutoff,
                    )
                ).limit(1)
                if (await session.execute(stmt)).scalar_one_or_none():
                    rec["_just_saved"] = False
                    continue

                tp1 = None
                sig = rec.get("signal") or {}
                if isinstance(sig, dict):
                    tp1 = sig.get("tp1")

                snap = RecommendationSnapshot(
                    symbol=rec["symbol"],
                    timeframe=rec["timeframe"],
                    tier=rec["tier"],
                    direction=rec["direction"],
                    entry=float(rec["entry"]),
                    stop_loss=float(rec["stop_loss"]),
                    tp1=float(tp1) if tp1 is not None else None,
                    tp2=float(rec["tp2"]),
                    score=float(rec["score"]),
                    risk_reward=float(rec["risk_reward"]),
                    leverage=int(rec.get("leverage", 1)),
                    risk_pct=float(rec.get("risk_pct", 1.0)),
                    stop_distance_pct=float(rec.get("stop_distance_pct", 0.0)),
                    status=WIDE_DISPLAY_STATUS,
                    created_at=now,
                    features=_extract_features(rec, now, _regime_label),
                )
                session.add(snap)
                rec["_just_saved"] = True
                inserted += 1
            except Exception as e:
                log.warning(f"[wide] falha ao salvar {rec.get('symbol')}: {e}")
        await session.commit()

    if inserted:
        log.info(f"[wide] snapshots display persistidos: {inserted}")
    return inserted


async def expire_open_snapshot(symbol: str, direction: str, reason: str = "flip_advisory") -> int:
    """Marca como 'expired' os snapshots OPEN de (symbol, direction).

    Chamado quando o flip é REJEITADO (advisory): a rec oposta foi deixada
    fluir por save_recommendations (pra alimentar o avaliador de flip), mas
    como o flip não disparou, não queremos poluir o painel 'Abertos' com um
    par long+short do mesmo símbolo. Expira a rec oposta — sem afetar o trade
    real, que continua aberto. Retorna quantos snapshots foram expirados."""
    if not DB_ENABLED:
        return 0
    try:
        async with get_session() as session:
            stmt = (
                update(RecommendationSnapshot)
                .where(
                    and_(
                        RecommendationSnapshot.symbol == symbol,
                        RecommendationSnapshot.direction == direction,
                        RecommendationSnapshot.status == "open",
                    )
                )
                .values(status="expired", outcome_at=datetime.now(timezone.utc))
            )
            res = await session.execute(stmt)
            await session.commit()
            n = res.rowcount or 0
            if n:
                log.debug(
                    f"[snapshot] expirado {n} snapshot(s) {symbol} {direction} ({reason})"
                )
            return n
    except Exception as e:
        log.warning(f"[snapshot] expire_open_snapshot falhou {symbol} {direction}: {e}")
        return 0


async def filter_keys_with_open_snapshot(keys) -> set:
    """Dado um iterável de chaves (symbol, timeframe, direction), retorna o
    SUBCONJUNTO que tem um snapshot 'open' agora no DB.

    Usado pelo scan loop pra NÃO empurrar push de uma rec que nasceu e já foi
    expirada no MESMO ciclo — caso clássico: flip_advisory (rec oposta a uma
    posição aberta, flip negado → expire_open_snapshot) e no-data. Sem isso, o
    push é montado antes da expiração e o usuário recebe a notificação de um
    trade que já aparece 'expirado'. Fail-open: em erro, devolve todas as
    chaves (não bloqueia push legítimo)."""
    want = {(s, tf, d) for (s, tf, d) in keys}
    if not DB_ENABLED or not want:
        return want
    try:
        syms = {k[0] for k in want}
        async with get_session() as session:
            stmt = select(
                RecommendationSnapshot.symbol,
                RecommendationSnapshot.timeframe,
                RecommendationSnapshot.direction,
            ).where(
                and_(
                    RecommendationSnapshot.symbol.in_(syms),
                    RecommendationSnapshot.status == "open",
                )
            )
            rows = (await session.execute(stmt)).all()
        have = {(r[0], r[1], r[2]) for r in rows}
        return want & have
    except Exception as e:
        log.warning(f"[snapshot] filter_keys_with_open_snapshot falhou: {e}")
        return want


def _atr_abs(snap: RecommendationSnapshot) -> Optional[float]:
    """ATR absoluto do setup, derivado das features (atr_pct × entry)."""
    feats = snap.features or {}
    atr_pct = feats.get("atr_pct")
    if atr_pct is None or snap.entry is None:
        return None
    try:
        return float(atr_pct) / 100.0 * float(snap.entry)
    except Exception:
        return None


def _classify_outcome_candles(snap: RecommendationSnapshot, df_window) -> Optional[tuple]:
    """
    Steps 2a+2b v2: processa candles em ordem cronológica.

    Lógica (v2 — BE+ assimétrico + buffer):
      • Se TP1 toca → fecha 50% (+1R parcial). Stop EFETIVO da metade restante
        vira BE+ lock (entry + 0.2×(tp1-entry)) = piso garantindo +0.2R.
      • Trail por ATR (K=2.2) só "aperta" o stop acima do BE+ se o peak
        avançou ≥0.5×ATR além do TP1 (buffer de ativação).
      • Se TP2 toca a qualquer momento → fecha como won_tp2 (+1.5R total).
      • Se stop ORIGINAL bate antes de TP1 → lost (-1R, posição cheia).
      • Se stop EFETIVO (BE+/trail) bate APÓS TP1 → won_tp1_be (+0.6R).

    Retorna uma das opções:
      ("won_tp2", price, +1.5, tp1_just_hit_bool, new_peak)   → lucro máximo
      ("won_tp1_be", price, +0.5, tp1_just_hit_bool, new_peak) → trail/BE
      ("lost", stop_loss, -1.0, False, None)                   → stop original
      ("open_after_tp1", None, None, True, new_peak)           → segue aberto
      ("open_update", None, None, False, new_peak)             → só atualiza peak
      None                                                      → segue aberto

    `new_peak` é o pico do preço a favor desde TP1 hit (None se ainda não houve
    TP1). O caller persiste em snap.peak_price_since_tp1.

    Regra conservadora: na MESMA vela, stop tem prioridade SE TP1 ainda não
    foi marcado em rounds anteriores.
    """
    if df_window is None or df_window.empty:
        return None

    is_long = snap.direction == "long"
    tp1_already = snap.tp1_hit_at is not None
    tp1_hit_now = False
    peak = snap.peak_price_since_tp1  # pode ser None
    atr = _atr_abs(snap)  # pode ser None — sem ATR, vira só BE puro

    for _, c in df_window.iterrows():
        h = float(c["high"])
        l = float(c["low"])
        cl = float(c["close"])

        # Atualiza peak se TP1 já foi (passado ou agora)
        if tp1_already or tp1_hit_now:
            cand_peak = h if is_long else l
            if peak is None:
                peak = cand_peak
            else:
                peak = max(peak, cand_peak) if is_long else min(peak, cand_peak)

        # Stop efetivo (v2 — BE+ assimétrico + trail com buffer de ativação):
        #   • Antes de TP1: stop original
        #   • Após TP1: PISO = BE+ lock (entry + 0.2 × (tp1-entry)) — garante
        #     mínimo +0.2R no pior caso. Trail dinâmico só "aperta" o stop
        #     ACIMA do BE+ se peak avançou ≥0.5×ATR além do TP1 (buffer).
        if tp1_already or tp1_hit_now:
            if snap.tp1 is not None:
                tp1_distance = snap.tp1 - snap.entry  # signed
                be_plus = snap.entry + BE_PLUS_LOCK_R * tp1_distance
            else:
                be_plus = snap.entry
            if atr is not None and peak is not None:
                # Trail só ativa depois do peak avançar além do buffer
                if is_long:
                    activation_threshold = (snap.tp1 or snap.entry) + TRAIL_ACTIVATION_BUFFER_ATR * atr
                    if peak >= activation_threshold:
                        trail = peak - ATR_TRAIL_K * atr
                        effective_stop = max(be_plus, trail)
                    else:
                        effective_stop = be_plus
                else:
                    activation_threshold = (snap.tp1 or snap.entry) - TRAIL_ACTIVATION_BUFFER_ATR * atr
                    if peak <= activation_threshold:
                        trail = peak + ATR_TRAIL_K * atr
                        effective_stop = min(be_plus, trail)
                    else:
                        effective_stop = be_plus
            else:
                effective_stop = be_plus
        else:
            effective_stop = snap.stop_loss

        # Stop ORIGINAL (pre-TP1) é close-based: só conta loss se a vela
        # FECHAR além do stop. Wick que toca e volta NÃO é stop. Em cripto,
        # wicks de 0.3-1% em pivôs são rotineiros — usar low/high pra stop
        # vira hunt de liquidez.
        # Stop POST-TP1 (BE+ / trail) permanece wick-based: protege lucro,
        # comportamento de stop order real em exchange.
        post_tp1 = tp1_already or tp1_hit_now
        if is_long:
            if post_tp1:
                stop_hit = l <= effective_stop
            else:
                stop_hit = cl <= effective_stop
            tp1_hit = (snap.tp1 is not None) and (h >= snap.tp1)
            tp2_hit = h >= snap.tp2
        else:
            if post_tp1:
                stop_hit = h >= effective_stop
            else:
                stop_hit = cl >= effective_stop
            tp1_hit = (snap.tp1 is not None) and (l <= snap.tp1)
            tp2_hit = l <= snap.tp2

        # Stop original antes de TP1 → loss cheio (worst-case)
        if stop_hit and not (tp1_already or tp1_hit_now):
            return ("lost", snap.stop_loss, REALIZED_R_STOP, False, None)

        # TP2 a qualquer momento → won_tp2 (lucro max). Se TP1 e TP2 batem
        # na MESMA vela sem TP1 prévio, ambos efeitos contam.
        if tp2_hit:
            # Garante que peak refletiu o evento se TP1 foi marcado agora
            if (tp1_hit and not tp1_already) or tp1_hit_now:
                tp1_hit_now = True
                if atr is not None:
                    cand_peak = h if is_long else l
                    peak = cand_peak if peak is None else (max(peak, cand_peak) if is_long else min(peak, cand_peak))
            return ("won_tp2", snap.tp2, REALIZED_R_TP2, tp1_hit_now, peak)

        # TP1 acabou de bater nesta vela (e não tinha batido antes)
        if tp1_hit and not (tp1_already or tp1_hit_now):
            tp1_hit_now = True
            # Inicializa peak com o high (long) / low (short) da vela
            cand_peak = h if is_long else l
            peak = cand_peak if peak is None else (max(peak, cand_peak) if is_long else min(peak, cand_peak))
            # Continua iterando — pode bater TP2 ou stop trail nesta janela
            continue

        # Stop trail/BE bate após TP1 já estar marcado
        if stop_hit and (tp1_already or tp1_hit_now):
            # Saída efetiva é o stop trail/BE
            return ("won_tp1_be", effective_stop, REALIZED_R_BREAKEVEN, tp1_hit_now, peak)

    # Não fechou. Se TP1 acabou de ser tocado, sinaliza com peak novo.
    if tp1_hit_now:
        return ("open_after_tp1", None, None, True, peak)
    # Se TP1 já era hit no passado, peak pode ter mudado — sinaliza update
    if tp1_already and peak != snap.peak_price_since_tp1:
        return ("open_update", None, None, False, peak)
    return None


async def _resolver_fetch_ohlcv(symbol: str, timeframe: str = "5m", limit: int = 50):
    """Fetch de candles pro RESOLVER de snapshots, à prova de ponto-cego entre
    exchanges.

    O scan/geração roda em Binance Futures (via proxy) — a exchange onde o bot
    e o usuário operam de verdade. A resolução, porém, historicamente lia preço
    da OKX (binance_service), que NÃO lista várias alts novas/menores presentes
    na Binance (ex.: MIRA). Resultado: esses setups — reais e já notificados via
    push — eram marcados 'expired/void' em segundos só porque a OKX não tinha o
    candle ("fast-void" cross-exchange, ~88% dos expirados). Isso confundia o
    usuário (push válido → painel diz "expirado na mesma hora") E sujava a
    calibração com voids falsos.

    Estratégia: tenta OKX primeiro (barata, sem custo de proxy). Se vier vazio E
    o proxy Binance estiver ligado, tenta a Binance Futures (mesma fonte da
    geração). Só devolve vazio se AS DUAS falharem — aí sim o símbolo é
    genuinamente intrastreável. Carga extra no proxy é mínima: só as poucas
    moedas/dia que a OKX não enxerga caem no fallback."""
    from services.binance_service import fetch_ohlcv as _okx_fetch
    try:
        df = await _okx_fetch(symbol, timeframe, limit)
    except Exception:
        df = None
    if df is not None and not df.empty:
        return df
    # Fallback: a OKX não tinha o candle. Tenta a Binance (fonte da geração).
    try:
        from services import binance_futures_service as _bfs
        if _bfs.PROXY_ENABLED:
            df_b = await _bfs.fetch_ohlcv(symbol, timeframe, limit)
            if df_b is not None and not df_b.empty:
                return df_b
    except Exception as e:
        log.debug(f"[resolver] fallback Binance falhou {symbol}: {e}")
    # Devolve o df da OKX (vazio) pra preservar o fluxo de void existente.
    return df if df is not None else _empty_ohlcv_df()


def _empty_ohlcv_df():
    """DataFrame vazio com as colunas esperadas — fallback se a OKX estourou."""
    import pandas as pd
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


async def check_open_snapshots() -> int:
    """
    Roda periodicamente. Busca todos abertos, consulta preço high/low desde
    last_check_at (ou created_at), classifica outcome.

    Retorna quantos snapshots foram resolvidos nesta chamada.
    """
    if not DB_ENABLED:
        return 0

    resolved = 0
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status == "open"
        )
        result = await session.execute(stmt)
        open_snaps = result.scalars().all()

        for snap in open_snaps:
            try:
                # Time-stop por TF: se TP1 NÃO tocou ainda E passou o limite
                # do TF (ex: 4h pro 15m), encerra como expired (0R, sem perda
                # nem ganho). Evita capital travado em trade que não anda.
                # Se TP1 já tocou, ignora time-stop (deixa trail/TP2 cuidar).
                age = now - snap.created_at
                tf_limit_h = _time_stop_hours(snap.timeframe)
                if snap.tp1_hit_at is None and age > timedelta(hours=tf_limit_h):
                    snap.status = "expired"
                    snap.realized_r = 0.0
                    snap.outcome_at = now
                    snap.last_check_at = now  # time-stop REAL é outcome avaliado
                    log.info(
                        f"[time-stop] {snap.symbol} {snap.timeframe} {snap.direction} "
                        f"expirado: {age.total_seconds()/3600:.1f}h sem TP1 "
                        f"(limite {tf_limit_h}h)"
                    )
                    resolved += 1
                    continue

                # Ceiling absoluto por TF (>= EXPIRY_HOURS=168h): independente de
                # TP1, fecha. Pra TFs longos (3d=504h) o teto acompanha o
                # time-stop do TF pra não cortar o hodl cedo demais.
                # Step 2a: se TP1 já tinha sido tocado, expira como won_tp1 (+0.5R)
                # — lucro parcial travado. Caso contrário, expired (0R).
                if age > timedelta(hours=_expiry_ceiling_hours(snap.timeframe)):
                    if snap.tp1_hit_at is not None:
                        snap.status = "won_tp1"
                        snap.outcome_price = snap.tp1
                        snap.realized_r = REALIZED_R_TP1
                        # Push: expirou pós-TP1, parcial travada.
                        try:
                            await notify_outcome(snap, "expired_tp1")
                        except Exception as e:
                            log.warning(f"notify_outcome expired_tp1 falhou: {e}")
                    else:
                        snap.status = "expired"
                        snap.realized_r = 0.0
                        # Não notifica expired sem TP1 (não aconteceu nada).
                    snap.outcome_at = now
                    snap.last_check_at = now  # teto absoluto REAL é outcome avaliado
                    resolved += 1
                    continue

                # Busca candles 5m desde o último check (no mínimo 1 candle)
                # Conservador: pega ~12 candles de 5m = 1h pra cobrir.
                # Resolver cross-exchange: OKX, com fallback Binance (ver
                # _resolver_fetch_ohlcv) — evita void falso de alts só-Binance.
                df = await _resolver_fetch_ohlcv(snap.symbol, "5m", 50)
                if df.empty:
                    # Sem candles. Pode ser falha transitória OU o par sumiu da
                    # fonte (ex.: símbolo que só existia na exchange anterior e
                    # não está no universo atual). Sem tratar, o snapshot
                    # congelaria "aberto" até o teto (EXPIRY_HOURS) — escondendo o
                    # resultado real e travando na UI. Se o símbolo
                    # confirmadamente NÃO está no universo da fonte, não há como
                    # resolver por preço: encerra como expired (sem dados) e
                    # segue. Indisponibilidade transitória (símbolo no universo,
                    # candle vazio pontual) cai no continue normal e tenta de novo.
                    unavailable = False
                    try:
                        from services.binance_service import get_perpetual_symbols
                        universe = set(await get_perpetual_symbols())
                        unavailable = bool(universe) and snap.symbol not in universe
                    except Exception as e:
                        log.warning(f"[no-data] checar universo {snap.symbol} falhou: {e}")
                    if unavailable:
                        snap.status = "expired"
                        snap.realized_r = 0.0
                        snap.outcome_at = now
                        snap.last_check_at = now
                        log.warning(
                            f"[no-data] {snap.symbol} {snap.timeframe} {snap.direction} "
                            f"encerrado expired: fora do universo da fonte "
                            f"(sem preço pra resolver outcome)"
                        )
                        resolved += 1
                    continue
                # Filtra apenas candles após last_check_at
                ref_ts = int((snap.last_check_at or snap.created_at).timestamp() * 1000)
                df_window = df[df["timestamp"] >= ref_ts]
                if df_window.empty:
                    df_window = df.tail(1)

                outcome = _classify_outcome_candles(snap, df_window)
                if outcome is not None:
                    status, price, r, tp1_just_hit, new_peak = outcome

                    if status == "open_after_tp1":
                        # Step 2a+2b: TP1 tocou agora — marca timestamp + peak,
                        # stop vira BE/trail, posição segue aberta.
                        snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                        log.info(
                            f"[step-2b] {snap.symbol} {snap.timeframe} {snap.direction} "
                            f"TP1 hit (parcial 50%) → trail ativo (peak={new_peak})"
                        )
                        # Push: TP1 parcial — usuário sabe que 50% saiu e
                        # stop subiu pra BE+.
                        try:
                            await notify_outcome(snap, "tp1_partial")
                        except Exception as e:
                            log.warning(f"notify_outcome tp1_partial falhou: {e}")
                    elif status == "open_update":
                        # Só atualiza peak (TP1 já era passado, mas peak melhorou)
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                    else:
                        # Fecha snapshot
                        snap.status = status
                        snap.outcome_price = price
                        snap.outcome_at = now
                        snap.realized_r = r
                        # Se TP1 bateu na MESMA janela (sem ter sido marcado
                        # antes), persiste tp1_hit_at e peak pra rastreio.
                        if tp1_just_hit and snap.tp1_hit_at is None:
                            snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                        resolved += 1
                        # Push de saída — emoji + R realizado.
                        # Mapeia status interno → evento user-facing.
                        event_map = {
                            "won_tp2": "tp2",
                            "won_tp1_be": "be_plus",
                            "won_tp1": "be_plus",  # tratado como saída pós-TP1
                            "lost": "lost",
                        }
                        ev = event_map.get(status)
                        if ev:
                            try:
                                await notify_outcome(snap, ev)
                            except Exception as e:
                                log.warning(f"notify_outcome {ev} falhou: {e}")
                        # Shadow #11.3: fecha trade sombra ligado a essa rec
                        try:
                            from services.shadow_trade_service import close_shadow_for_snapshot
                            await close_shadow_for_snapshot(snap)
                        except Exception as e:
                            log.warning(f"shadow close falhou: {e}")

                snap.last_check_at = now
            except Exception as e:
                log.warning(f"Erro checando snapshot {snap.id} ({snap.symbol}): {e}")

        await session.commit()

    if resolved:
        log.info(f"Snapshots resolvidos: {resolved}")
    return resolved


def _record_wide_outcome(snap, status: str, price, r, now) -> None:
    """
    Grava o resultado de OBSERVAÇÃO de um snapshot 'wide':
      • status resolvido vai pro namespace 'wide_*' (NUNCA os status reais);
      • realized_r fica SEMPRE NULL (não contamina nenhum agregador);
      • R/preço/saída ficam em features['wide_outcome'] (JSON), fonte de verdade
        do painel de observação.
    Reatribui o dict de features (não muta in-place) pra o JSON ser marcado dirty.
    """
    snap.status = status if status.startswith(WIDE_STATUS_PREFIX) else (WIDE_STATUS_PREFIX + status)
    snap.outcome_price = price
    snap.outcome_at = now
    snap.realized_r = None  # ISOLAMENTO: jamais escreve a coluna compartilhada
    feats = dict(snap.features or {})
    feats["wide_outcome"] = {
        "r": float(r) if r is not None else None,
        "price": float(price) if price is not None else None,
        "status": snap.status,
        "at": now.isoformat(),
    }
    snap.features = feats


async def check_wide_snapshots() -> int:
    """
    Opção B — rastreio do universo AMPLO p/ HISTÓRICO/observação.

    Espelha check_open_snapshots, MAS sobre os snapshots status=='wide' e SEM
    tocar em nada que alimente as estatísticas reais:
      • usa o MESMO motor de outcome (_classify_outcome_candles, time-stop,
        expiry, candles 5m) — mesma fidelidade do tracker real;
      • ao resolver, status vai pro namespace 'wide_*' e o R vai em
        features['wide_outcome']; realized_r fica NULL ⇒ invisível ao
        learner/risco/PnL POR CONSTRUÇÃO;
      • NÃO dispara notify_outcome nem close_shadow (não é dinheiro real, não
        deve poluir push/sombra);
      • limita a WIDE_TRACKING_MAX por ciclo (carga de candles no proxy),
        rotacionando pelos menos-recentemente-checados;
      • poda resolvidos 'wide_*' velhos (outcome_at < EXPIRY_HOURS) — o painel
        já filtra por janela de data, então não há porque acumular pra sempre.

    Gated por WIDE_TRACKING_ENABLED (default OFF). No-op se desligado.
    """
    if not DB_ENABLED or not WIDE_TRACKING_ENABLED:
        return 0

    resolved = 0
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Poda resolvidos 'wide_*' já fora da janela de exibição útil.
        try:
            await session.execute(
                delete(RecommendationSnapshot).where(
                    and_(
                        RecommendationSnapshot.status.in_(WIDE_RESOLVED_STATUSES),
                        RecommendationSnapshot.outcome_at < (now - timedelta(hours=EXPIRY_HOURS)),
                    )
                )
            )
        except Exception as e:
            log.warning(f"[wide-track] prune resolvidos falhou: {e}")

        # Abertos 'wide', menos-recentemente-checados primeiro, capado.
        stmt = (
            select(RecommendationSnapshot)
            .where(RecommendationSnapshot.status == WIDE_DISPLAY_STATUS)
            .order_by(RecommendationSnapshot.last_check_at.asc().nullsfirst())
            .limit(WIDE_TRACKING_MAX)
        )
        result = await session.execute(stmt)
        wide_snaps = result.scalars().all()

        for snap in wide_snaps:
            try:
                age = now - snap.created_at
                tf_limit_h = _time_stop_hours(snap.timeframe)
                # Time-stop sem TP1 → expired de observação (0R).
                if snap.tp1_hit_at is None and age > timedelta(hours=tf_limit_h):
                    _record_wide_outcome(snap, "wide_expired", None, 0.0, now)
                    resolved += 1
                    continue

                # Teto absoluto de expiração (por TF: >= EXPIRY_HOURS, estende
                # pra TFs longos como 3d=504h).
                if age > timedelta(hours=_expiry_ceiling_hours(snap.timeframe)):
                    if snap.tp1_hit_at is not None:
                        _record_wide_outcome(snap, "wide_won_tp1", snap.tp1, REALIZED_R_TP1, now)
                    else:
                        _record_wide_outcome(snap, "wide_expired", None, 0.0, now)
                    resolved += 1
                    continue

                df = await _resolver_fetch_ohlcv(snap.symbol, "5m", 50)
                if df.empty:
                    # Símbolo fora do universo da fonte → encerra como expired obs.
                    unavailable = False
                    try:
                        from services.binance_service import get_perpetual_symbols
                        universe = set(await get_perpetual_symbols())
                        unavailable = bool(universe) and snap.symbol not in universe
                    except Exception as e:
                        log.warning(f"[wide-track] checar universo {snap.symbol} falhou: {e}")
                    if unavailable:
                        _record_wide_outcome(snap, "wide_expired", None, 0.0, now)
                        snap.last_check_at = now
                        resolved += 1
                    continue

                ref_ts = int((snap.last_check_at or snap.created_at).timestamp() * 1000)
                df_window = df[df["timestamp"] >= ref_ts]
                if df_window.empty:
                    df_window = df.tail(1)

                outcome = _classify_outcome_candles(snap, df_window)
                if outcome is not None:
                    status, price, r, tp1_just_hit, new_peak = outcome
                    if status == "open_after_tp1":
                        # TP1 parcial: mantém status 'wide', marca tp1/peak — segue aberto.
                        snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                    elif status == "open_update":
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                    else:
                        # Resolveu: grava no namespace de observação.
                        if tp1_just_hit and snap.tp1_hit_at is None:
                            snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                        _record_wide_outcome(snap, WIDE_STATUS_PREFIX + status, price, r, now)
                        resolved += 1
                        # SEM notify_outcome / close_shadow: observação não é real.

                snap.last_check_at = now
            except Exception as e:
                log.warning(f"Erro checando wide {snap.id} ({snap.symbol}): {e}")

        await session.commit()

    if resolved:
        log.info(f"[wide-track] snapshots de observação resolvidos: {resolved}")
    return resolved


async def get_recently_stopped_symbols(hours: int = 6) -> set[str]:
    """
    Retorna o set de símbolos que tiveram stop ('lost') ou expiry sem TP1
    nas últimas N horas. Usado como cooldown — não recomendar de novo
    enquanto a tese técnica não 'resfria'.
    """
    if not DB_ENABLED:
        return set()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.symbol).where(
                RecommendationSnapshot.outcome_at >= cutoff,
                RecommendationSnapshot.status.in_(("lost", "expired")),
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}
    except Exception as e:
        log.warning(f"[cooldown] falha buscando símbolos estopados: {e}")
        return set()


async def get_daily_pnl(
    target_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Agrega P&L do dia especificado (ou hoje) — ou range [target_date, end_date].
    Considera snapshots cujo `outcome_at` cai no intervalo.
    """
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    if target_date is None:
        target_date = datetime.now(timezone.utc).date()
    if end_date is None:
        end_date = target_date

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)

    async with get_session() as session:
        # Snapshots resolvidos NESTE dia (outcome_at). O dia do fechamento
        # manda — trade aberto ontem mas que estopou hoje aparece em hoje.
        # Inclui expired pra ficar visível (não conta em win/loss, r=0).
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= day_start,
                RecommendationSnapshot.outcome_at < day_end,
                RecommendationSnapshot.status.in_(
                    ("won_tp1", "won_tp1_be", "won_tp2", "lost", "expired")
                ),
            )
        )
        result = await session.execute(stmt)
        snaps = result.scalars().all()

        # Snapshots ainda abertos — TODOS, independente do dia em que foram
        # criados. Trade de ontem que ainda não bateu TP/stop é tão relevante
        # quanto um aberto hoje (capital travado em ambos).
        open_stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status == "open"
        )
        open_snaps = (await session.execute(open_stmt)).scalars().all()
        open_count = len(open_snaps)

        # ── Opção B: observação (universo AMPLO, status 'wide'/'wide_*') ──
        # Coletados SÓ se houver rastreio ligado (senão as listas ficam vazias).
        # São retornados em chaves PRÓPRIAS, NUNCA somados ao summary do bot.
        wide_snaps = []
        wide_open_snaps = []
        if WIDE_TRACKING_ENABLED:
            wide_stmt = select(RecommendationSnapshot).where(
                and_(
                    RecommendationSnapshot.outcome_at >= day_start,
                    RecommendationSnapshot.outcome_at < day_end,
                    RecommendationSnapshot.status.in_(WIDE_RESOLVED_STATUSES),
                )
            )
            wide_snaps = (await session.execute(wide_stmt)).scalars().all()
            wide_open_stmt = select(RecommendationSnapshot).where(
                RecommendationSnapshot.status == WIDE_DISPLAY_STATUS
            )
            wide_open_snaps = (await session.execute(wide_open_stmt)).scalars().all()

    wins = [s for s in snaps if s.realized_r and s.realized_r > 0]
    losses = [s for s in snaps if s.realized_r and s.realized_r < 0]
    total_r = sum(s.realized_r or 0 for s in snaps)
    win_count = len(wins)
    loss_count = len(losses)
    total = win_count + loss_count
    win_rate = (win_count / total * 100) if total else 0

    # Veredito de qualidade do bot por snapshot — True/False/None.
    # Snapshots NOVOS trazem o veredito EXATO em features['bot_verdict_ok'].
    # Antigos (sem o campo) recomputam via exec_verdict a partir dos níveis
    # (gate de R:R aplica; P(TP1)/liquidez ficam no-op por falta de dado —
    # fail-soft, igual ao loop). Fonte única de regra = exec_verdict.
    def _bot_approved(s):
        feat = s.features or {}
        v = feat.get("bot_verdict_ok")
        if v is not None:
            return bool(v)
        try:
            from services.shadow_trade_service import exec_verdict
            return bool(exec_verdict({
                "entry": s.entry, "stop_loss": s.stop_loss,
                "tp1": s.tp1, "tp2": s.tp2,
            }).get("ok"))
        except Exception:
            return None

    # Calibração score→P(TP1)/P(TP2) — sync, lê do cache, fail-soft (None se
    # calib imatura). Mesma fonte do "P 70%" das Recomendações; expõe a métrica
    # no card de abertos pra dar paridade de decisão. risk_reward já é coluna;
    # confluence_pct vem do vetor de features. Tudo opcional no front.
    try:
        from services.calibration_service import (
            get_calibration, prob_tp1_for_score_sync, prob_tp2_for_score_sync,
        )
        # Aquece o cache de calibração (sync lê só dele). Barato: guardado por
        # TTL — quase sempre hit. Sem isso, num processo recém-deployado o
        # cache pode estar frio e P(TP1) sairia None pra todos os abertos.
        await get_calibration()
    except Exception:  # pragma: no cover — fail-soft
        prob_tp1_for_score_sync = lambda _s: None  # noqa: E731
        prob_tp2_for_score_sync = lambda _s: None  # noqa: E731

    def _decision_fields(s):
        """Campos de auxílio à decisão (paridade com o painel de Recomendações)."""
        feat = s.features or {}
        return {
            "risk_reward": s.risk_reward,
            "prob_tp1": prob_tp1_for_score_sync(s.score),
            "prob_tp2": prob_tp2_for_score_sync(s.score),
            "confluence_pct": feat.get("confluence_pct"),
        }

    # Detalhe por trade (resolvido + aberto, em listas separadas)
    def _serialize(s):
        return {
            **_decision_fields(s),
            "bot_approved": _bot_approved(s),
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "tier": s.tier,
            "direction": s.direction,
            "entry": s.entry,
            "stop_loss": s.stop_loss,
            "tp1": s.tp1,
            "tp2": s.tp2,
            "leverage": s.leverage,
            "status": s.status,
            "realized_r": s.realized_r,
            "risk_pct": s.risk_pct,
            "score": s.score,
            # VOID = 'expired' que NUNCA teve avaliação justa. NÃO é time-stop
            # real (esse só dispara com age >= 1h). Dois produtores: no-data /
            # fora do universo (expira no 1º check, segundos após criar — set
            # last_check_at) e flip_advisory (last_check_at NULL). Discriminador
            # robusto = resolveu em < 30min OU last_check_at NULL. Capital nunca
            # ficou exposto. Front rotula distinto; já fora da calibração.
            "void": (
                s.status == "expired" and (
                    s.last_check_at is None or (
                        s.outcome_at is not None and s.created_at is not None
                        and (s.outcome_at - s.created_at) < timedelta(minutes=30)
                    )
                )
            ),
            # Padrões detectados no momento da criação (nomes de tipo) + zona de
            # entrada — pra o card de Abertos nomear o padrão igual Recomendações.
            "patterns": (s.features or {}).get("patterns") or [],
            "entry_zone_type": (s.features or {}).get("entry_zone_type"),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "outcome_at": s.outcome_at.isoformat() if s.outcome_at else None,
            "tp1_hit_at": s.tp1_hit_at.isoformat() if s.tp1_hit_at else None,
        }

    trades = [_serialize(s) for s in sorted(snaps, key=lambda x: x.outcome_at or x.created_at)]
    open_trades = [_serialize(s) for s in sorted(open_snaps, key=lambda x: x.created_at)]

    # Serializer de OBSERVAÇÃO: tira o prefixo 'wide_' do status (pra o painel
    # bucketizar em vencedor/perdedor/aberto igual aos reais) e puxa o R de
    # features['wide_outcome'] — a coluna realized_r é sempre NULL nesses.
    # Marca origin='observation' pro front exibir o selo 👁 e isolar das stats.
    def _serialize_wide(s):
        out = (s.features or {}).get("wide_outcome") or {}
        raw_status = s.status or ""
        disp_status = raw_status[len(WIDE_STATUS_PREFIX):] if raw_status.startswith(WIDE_STATUS_PREFIX) else (
            "open" if raw_status == WIDE_DISPLAY_STATUS else raw_status
        )
        return {
            **_decision_fields(s),
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "tier": s.tier,
            "direction": s.direction,
            "entry": s.entry,
            "stop_loss": s.stop_loss,
            "tp1": s.tp1,
            "tp2": s.tp2,
            "leverage": s.leverage,
            "status": disp_status,
            "realized_r": out.get("r"),  # R de observação (de features, não da coluna)
            "risk_pct": s.risk_pct,
            "score": s.score,
            "bot_approved": _bot_approved(s),
            "origin": "observation",
            "patterns": (s.features or {}).get("patterns") or [],
            "entry_zone_type": (s.features or {}).get("entry_zone_type"),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "outcome_at": s.outcome_at.isoformat() if s.outcome_at else None,
            "tp1_hit_at": s.tp1_hit_at.isoformat() if s.tp1_hit_at else None,
        }

    wide_trades = [_serialize_wide(s) for s in sorted(wide_snaps, key=lambda x: x.outcome_at or x.created_at)]
    wide_open_trades = [_serialize_wide(s) for s in sorted(wide_open_snaps, key=lambda x: x.created_at)]

    # Soma o % real da banca afetado no dia (cada trade tem seu risco próprio:
    # A+=1.5%, A=1%, B=0.5%). Não é total_r × risk_pct[0] — isso só vale se
    # todos os trades fossem do mesmo tier. Aqui somamos por trade.
    total_pct_banca = sum((s.realized_r or 0) * s.risk_pct for s in snaps)

    return {
        "enabled": True,
        "date": target_date.isoformat(),
        "end_date": end_date.isoformat(),
        "is_range": target_date != end_date,
        "summary": {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": round(win_rate, 1),
            "total_r": round(total_r, 2),
            "total_pct_banca": round(total_pct_banca, 3),
            "still_open": open_count,
        },
        "trades": trades,
        "open_trades": open_trades,
        # Opção B — observação (universo amplo). Listas PRÓPRIAS, fora do summary
        # do bot. Vazias quando WIDE_TRACKING_ENABLED=OFF.
        "wide_trades": wide_trades,
        "wide_open_trades": wide_open_trades,
    }


async def get_history_stats(days: int = 30) -> Dict[str, Any]:
    """Estatísticas dos últimos N dias — alimenta o planejador da banca."""
    if not DB_ENABLED:
        return {"enabled": False}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
            )
        )
        snaps = (await session.execute(stmt)).scalars().all()

    if not snaps:
        return {"enabled": True, "days": days, "trades": 0}

    wins = [s for s in snaps if (s.realized_r or 0) > 0]
    losses = [s for s in snaps if (s.realized_r or 0) < 0]
    total = len(snaps)
    win_rate = len(wins) / total if total else 0
    avg_win_r = sum(s.realized_r or 0 for s in wins) / len(wins) if wins else 0
    trades_per_day = total / days

    # Risco médio por trade
    avg_risk_pct = sum(s.risk_pct for s in snaps) / total if total else 1.0

    # E[R] por trade = win_rate * avg_win_R + (1-win_rate) * (-1)
    expected_r = win_rate * avg_win_r - (1 - win_rate) * 1.0
    # Retorno diário esperado em fração da banca = trades_dia × risk_pct × E[R]
    daily_return = trades_per_day * (avg_risk_pct / 100) * expected_r

    return {
        "enabled": True,
        "days": days,
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win_r": round(avg_win_r, 2),
        "expected_r": round(expected_r, 3),
        "trades_per_day": round(trades_per_day, 2),
        "avg_risk_pct": round(avg_risk_pct, 2),
        "expected_daily_return_pct": round(daily_return * 100, 3),
    }
