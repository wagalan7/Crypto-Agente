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
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, func, update

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

# ── Configuração ─────────────────────────────────────────────────────────
DEDUP_WINDOW_HOURS = 2       # mesma rec não entra 2× nesse intervalo
EXPIRY_HOURS = 48            # ceiling absoluto — qualquer trade > 48h é encerrado

# Time-stop por timeframe: se TP1 não bater em N candles, encerra o trade
# (evita "morte lenta" que trava capital sem stop nem TP). Cada TF tem
# horizonte próprio — scalp deve resolver em horas, swing aguenta dias.
# Valor = horas máximas SEM tocar TP1. Se TP1 já tocou, NÃO aplica
# (trade está em lucro, deixa o trail/TP2 cuidar).
TIME_STOP_HOURS_BY_TF = {
    "1m": 1, "3m": 2, "5m": 3, "15m": 4, "30m": 8,
    "1h": 12, "2h": 24, "4h": 36, "6h": 48,
    "8h": 48, "12h": 48, "1d": 48,  # cappado pelo EXPIRY_HOURS
}


def _time_stop_hours(tf: str) -> float:
    """Horas máximas sem tocar TP1 antes de encerrar por time-stop."""
    return float(TIME_STOP_HOURS_BY_TF.get(tf, EXPIRY_HOURS))
# ── R esperado (Step 2b: parcial 50% no TP1) ─────────────────────────────
# Premissa: ao tocar TP1, fecha 50% da posição (+1R em metade), restante segue
# com stop em entry e trail por ATR. O R reportado é a MÉDIA ponderada das
# duas metades:
#   • Parcial sai em TP1 (+1R) → metade = +0.5R
#   • Final em TP2 (+2R)       → metade = +1.0R  → total +1.5R (won_tp2)
#   • Final em entry/trail (0R) → metade = 0R    → total +0.5R (won_tp1_be)
#   • Final expira após TP1     → conservador 0R → total +0.5R (won_tp1)
# Stop original (antes de TP1) = -1R cheio (não houve parcial).
REALIZED_R_TP1 = 0.5           # 50% TP1 + 50% breakeven (conservador no expiry após TP1)
REALIZED_R_TP2 = 1.5           # 50% TP1 + 50% TP2
REALIZED_R_STOP = -1.0         # stop original (antes de TP1)
REALIZED_R_BREAKEVEN = 0.5     # 50% TP1 + 50% entry (stop em BE bate ou trail aciona)

# Trail por ATR após TP1 hit. Stop trail = peak ± K × ATR, com piso em entry.
ATR_TRAIL_K = 1.5


def _extract_features(rec: Dict[str, Any], created_at: datetime) -> Dict[str, Any]:
    """Captura vetor de features pro learning loop. Robust a campos ausentes."""
    sig = rec.get("signal") or {}
    if not isinstance(sig, dict):
        return {"hour_utc": created_at.hour, "day_of_week": created_at.weekday()}

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
    }


async def save_recommendations(recommendations: List[Dict[str, Any]]) -> int:
    """
    Salva snapshots de recomendações novas (desduplicadas).
    Retorna quantos foram efetivamente inseridos.
    """
    if not DB_ENABLED or not recommendations:
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_WINDOW_HOURS)

    async with get_session() as session:
        for rec in recommendations:
            try:
                # Dedup: existe registro recente do mesmo setup?
                stmt = select(RecommendationSnapshot.id).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.timeframe == rec["timeframe"],
                        RecommendationSnapshot.direction == rec["direction"],
                        RecommendationSnapshot.created_at >= cutoff,
                    )
                ).limit(1)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing:
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
                    features=_extract_features(rec, now),
                )
                session.add(snap)
                inserted += 1
            except Exception as e:
                log.warning(f"Falha ao salvar snapshot {rec.get('symbol')}: {e}")
        await session.commit()

    if inserted:
        log.info(f"Snapshots persistidos: {inserted}")
    return inserted


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
    Steps 2a+2b: processa candles em ordem cronológica.

    Lógica:
      • Se TP1 toca → fecha 50% (+1R parcial), stop sobe pra entry, restante
        passa a trailar pelo ATR (peak ± K×ATR, piso em entry).
      • Se TP2 toca a qualquer momento → fecha como won_tp2 (+1.5R total,
        incluindo o parcial).
      • Se stop ORIGINAL bate antes de TP1 → lost (-1R, posição cheia).
      • Se stop EFETIVO (entry ou trail) bate APÓS TP1 → won_tp1_be (+0.5R).

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

        # Atualiza peak se TP1 já foi (passado ou agora)
        if tp1_already or tp1_hit_now:
            cand_peak = h if is_long else l
            if peak is None:
                peak = cand_peak
            else:
                peak = max(peak, cand_peak) if is_long else min(peak, cand_peak)

        # Stop efetivo:
        #   • Antes de TP1: stop original
        #   • Após TP1 sem ATR: stop = entry (BE puro)
        #   • Após TP1 com ATR: max(entry, peak - K×ATR) pra long;
        #                       min(entry, peak + K×ATR) pra short
        if tp1_already or tp1_hit_now:
            if atr is not None and peak is not None:
                if is_long:
                    trail = peak - ATR_TRAIL_K * atr
                    effective_stop = max(snap.entry, trail)
                else:
                    trail = peak + ATR_TRAIL_K * atr
                    effective_stop = min(snap.entry, trail)
            else:
                effective_stop = snap.entry
        else:
            effective_stop = snap.stop_loss

        if is_long:
            stop_hit = l <= effective_stop
            tp1_hit = (snap.tp1 is not None) and (h >= snap.tp1)
            tp2_hit = h >= snap.tp2
        else:
            stop_hit = h >= effective_stop
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


async def check_open_snapshots() -> int:
    """
    Roda periodicamente. Busca todos abertos, consulta preço high/low desde
    last_check_at (ou created_at), classifica outcome.

    Retorna quantos snapshots foram resolvidos nesta chamada.
    """
    if not DB_ENABLED:
        return 0

    # Import lazy pra evitar ciclo
    from services.binance_service import fetch_ohlcv

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
                    log.info(
                        f"[time-stop] {snap.symbol} {snap.timeframe} {snap.direction} "
                        f"expirado: {age.total_seconds()/3600:.1f}h sem TP1 "
                        f"(limite {tf_limit_h}h)"
                    )
                    resolved += 1
                    continue

                # Ceiling absoluto (48h): independente de TF, fecha.
                # Step 2a: se TP1 já tinha sido tocado, expira como won_tp1 (+0.5R)
                # — lucro parcial travado. Caso contrário, expired (0R).
                if age > timedelta(hours=EXPIRY_HOURS):
                    if snap.tp1_hit_at is not None:
                        snap.status = "won_tp1"
                        snap.outcome_price = snap.tp1
                        snap.realized_r = REALIZED_R_TP1
                    else:
                        snap.status = "expired"
                        snap.realized_r = 0.0
                    snap.outcome_at = now
                    resolved += 1
                    continue

                # Busca candles 5m desde o último check (no mínimo 1 candle)
                # Conservador: pega ~12 candles de 5m = 1h pra cobrir.
                df = await fetch_ohlcv(snap.symbol, "5m", 50)
                if df.empty:
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

                snap.last_check_at = now
            except Exception as e:
                log.warning(f"Erro checando snapshot {snap.id} ({snap.symbol}): {e}")

        await session.commit()

    if resolved:
        log.info(f"Snapshots resolvidos: {resolved}")
    return resolved


async def get_daily_pnl(target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Agrega P&L do dia especificado (ou hoje).
    Considera snapshots cujo `outcome_at` cai no dia, ignorando expired/open.
    """
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    async with get_session() as session:
        # Snapshots resolvidos hoje (won_tp1, won_tp2, lost)
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= day_start,
                RecommendationSnapshot.outcome_at < day_end,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
            )
        )
        result = await session.execute(stmt)
        snaps = result.scalars().all()

        # Snapshots ainda abertos criados hoje (detalhe completo pro drill-down)
        open_stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.created_at >= day_start,
                RecommendationSnapshot.created_at < day_end,
                RecommendationSnapshot.status == "open",
            )
        )
        open_snaps = (await session.execute(open_stmt)).scalars().all()
        open_count = len(open_snaps)

    wins = [s for s in snaps if s.realized_r and s.realized_r > 0]
    losses = [s for s in snaps if s.realized_r and s.realized_r < 0]
    total_r = sum(s.realized_r or 0 for s in snaps)
    win_count = len(wins)
    loss_count = len(losses)
    total = win_count + loss_count
    win_rate = (win_count / total * 100) if total else 0

    # Detalhe por trade (resolvido + aberto, em listas separadas)
    def _serialize(s):
        return {
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
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "outcome_at": s.outcome_at.isoformat() if s.outcome_at else None,
            "tp1_hit_at": s.tp1_hit_at.isoformat() if s.tp1_hit_at else None,
        }

    trades = [_serialize(s) for s in sorted(snaps, key=lambda x: x.outcome_at or x.created_at)]
    open_trades = [_serialize(s) for s in sorted(open_snaps, key=lambda x: x.created_at)]

    # Soma o % real da banca afetado no dia (cada trade tem seu risco próprio:
    # A+=1.5%, A=1%, B=0.5%). Não é total_r × risk_pct[0] — isso só vale se
    # todos os trades fossem do mesmo tier. Aqui somamos por trade.
    total_pct_banca = sum((s.realized_r or 0) * s.risk_pct for s in snaps)

    return {
        "enabled": True,
        "date": target_date.isoformat(),
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
