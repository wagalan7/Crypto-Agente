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
EXPIRY_HOURS = 48            # snapshots abertos viram "expired" depois disso
REALIZED_R_TP1 = 1.0          # TP1 atingido e posição encerrada com lucro parcial (expiry após TP1)
REALIZED_R_TP2 = 2.0          # TP2 cheio
REALIZED_R_STOP = -1.0        # stop original (antes de TP1)
REALIZED_R_BREAKEVEN = 0.0    # stop em entry após TP1 — Step 2a


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


def _classify_outcome_candles(snap: RecommendationSnapshot, df_window) -> Optional[tuple]:
    """
    Step 2a: processa candles em ordem cronológica e aplica a regra:
      • Se TP1 toca → stop sobe pra entry (breakeven) nas próximas velas.
      • Se TP2 toca → fecha como won_tp2 (+2R).
      • Se stop ORIGINAL bate antes de TP1 → lost (-1R).
      • Se stop EFETIVO (entry) bate APÓS TP1 → won_tp1_be (0R, breakeven).

    Retorna uma das opções:
      ("won_tp2", price, +2.0, tp1_just_hit_bool)        → fecha lucro máximo
      ("won_tp1_be", entry, 0.0, tp1_just_hit_bool)      → breakeven após TP1
      ("lost", stop_loss, -1.0, False)                   → stop original
      ("open_after_tp1", None, None, True)               → ainda aberto, MAS tocou TP1 agora
      None                                                → segue aberto, sem evento

    Regra conservadora mantida: se uma MESMA vela toca stop e tp1, assume que
    stop bateu antes (pior caso) — exceto se TP1 já tinha sido marcado em
    rounds anteriores.
    """
    if df_window is None or df_window.empty:
        return None

    is_long = snap.direction == "long"
    tp1_already = snap.tp1_hit_at is not None
    tp1_hit_now = False  # marca se TP1 acabou de bater nesta janela

    for _, c in df_window.iterrows():
        h = float(c["high"])
        l = float(c["low"])

        # Stop efetivo: se TP1 (passado OU agora) já bateu, stop = entry
        effective_stop = snap.entry if (tp1_already or tp1_hit_now) else snap.stop_loss

        if is_long:
            stop_hit = l <= effective_stop
            tp1_hit = (snap.tp1 is not None) and (h >= snap.tp1)
            tp2_hit = h >= snap.tp2
        else:
            stop_hit = h >= effective_stop
            tp1_hit = (snap.tp1 is not None) and (l <= snap.tp1)
            tp2_hit = l <= snap.tp2

        # Conservador: na MESMA vela, stop tem prioridade SE TP1 ainda não foi
        # marcado em rounds anteriores. Se TP1 já era hit no passado, stop em
        # entry pode bater junto com TP2 — aí ainda assim damos prioridade pro
        # breakeven (worst-case) só se TP2 não bater junto.
        if stop_hit and not (tp1_already or tp1_hit_now):
            # Stop original antes de TP1 → loss puro, mesmo se TP1/TP2 batem
            # na mesma vela (worst-case assume stop primeiro).
            return ("lost", snap.stop_loss, REALIZED_R_STOP, False)

        # Após (ou junto com) TP1 já marcado, TP2 ganha prioridade sobre BE
        if tp2_hit:
            return ("won_tp2", snap.tp2, REALIZED_R_TP2, tp1_hit_now)

        # TP1 acabou de bater nesta vela
        if tp1_hit and not (tp1_already or tp1_hit_now):
            tp1_hit_now = True
            # NÃO retorna ainda — continua iterando candles, agora com stop
            # efetivo no entry. Pode bater TP2 ou voltar pro entry no próprio
            # window.
            continue

        # Stop efetivo (= entry) bate após TP1 já estar marcado
        if stop_hit and (tp1_already or tp1_hit_now):
            return ("won_tp1_be", snap.entry, REALIZED_R_BREAKEVEN, tp1_hit_now)

    # Não fechou. Reporta se TP1 acabou de ser tocado agora.
    if tp1_hit_now:
        return ("open_after_tp1", None, None, True)
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
                # Expiração: passou de EXPIRY_HOURS desde criado.
                # Step 2a: se TP1 já tinha sido tocado, expira como won_tp1 (+1R)
                # — lucro parcial travado. Caso contrário, expired (0R).
                if (now - snap.created_at) > timedelta(hours=EXPIRY_HOURS):
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
                    status, price, r, tp1_just_hit = outcome

                    if status == "open_after_tp1":
                        # Step 2a: TP1 tocou agora — marca timestamp, stop sobe
                        # pra entry, posição segue aberta esperando TP2 ou BE.
                        snap.tp1_hit_at = now
                        log.info(
                            f"[breakeven] {snap.symbol} {snap.timeframe} {snap.direction} "
                            f"TP1 hit → stop movido pra entry ({snap.entry})"
                        )
                    else:
                        # Fecha snapshot
                        snap.status = status
                        snap.outcome_price = price
                        snap.outcome_at = now
                        snap.realized_r = r
                        # Se foi won_tp2 e TP1 bateu na MESMA janela (sem ter sido
                        # marcado antes), grava também tp1_hit_at pra rastreio.
                        if tp1_just_hit and snap.tp1_hit_at is None:
                            snap.tp1_hit_at = now
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

        # Snapshots ainda abertos criados hoje
        open_stmt = select(func.count(RecommendationSnapshot.id)).where(
            and_(
                RecommendationSnapshot.created_at >= day_start,
                RecommendationSnapshot.created_at < day_end,
                RecommendationSnapshot.status == "open",
            )
        )
        open_count = (await session.execute(open_stmt)).scalar() or 0

    wins = [s for s in snaps if s.realized_r and s.realized_r > 0]
    losses = [s for s in snaps if s.realized_r and s.realized_r < 0]
    total_r = sum(s.realized_r or 0 for s in snaps)
    win_count = len(wins)
    loss_count = len(losses)
    total = win_count + loss_count
    win_rate = (win_count / total * 100) if total else 0

    # Detalhe por trade
    trades = []
    for s in sorted(snaps, key=lambda x: x.outcome_at or x.created_at):
        trades.append({
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "tier": s.tier,
            "direction": s.direction,
            "entry": s.entry,
            "stop_loss": s.stop_loss,
            "tp2": s.tp2,
            "leverage": s.leverage,
            "status": s.status,
            "realized_r": s.realized_r,
            "risk_pct": s.risk_pct,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "outcome_at": s.outcome_at.isoformat() if s.outcome_at else None,
        })

    return {
        "enabled": True,
        "date": target_date.isoformat(),
        "summary": {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": round(win_rate, 1),
            "total_r": round(total_r, 2),
            "still_open": open_count,
        },
        "trades": trades,
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
